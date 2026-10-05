import asyncio
from collections import deque
import struct
from types import SimpleNamespace

import pytest

from engine.qwen_boundary import BoundaryChunker
from engine.sessions import SessionManager, StreamSession
from engine.settings import EngineError


def pcm(seconds, amplitude=1000):
    return struct.pack('<h', amplitude) * round(seconds * 16000)


def feed(chunker, data):
    requests=[]
    for offset in range(0,len(data),3200):
        chunker.feed(data[offset:offset+3200])
        while request:=chunker.next_request():
            requests.append(request)
            chunker.resolve(request)
    return requests


def test_forced_boundary_rechecks_the_identical_audio_start_with_one_second_more():
    chunker=BoundaryChunker()
    requests=feed(chunker,pcm(5))
    assert [r.probe for r in requests]==[True,False]
    assert [r.rechecked for r in requests]==[False,True]
    assert [(r.start_sample,r.end_sample) for r in requests]==[(0,64000),(0,80000)]
    assert requests[1].pcm.startswith(requests[0].pcm)


def test_natural_pause_does_not_wait_for_an_extra_second_or_decode_twice():
    requests=feed(BoundaryChunker(),pcm(.4)+pcm(.5,0))
    assert len(requests)==1 and not requests[0].probe and not requests[0].rechecked
    assert len(requests[0].pcm)==28800


def test_natural_pause_during_recheck_finishes_before_the_one_second_cap():
    requests=feed(BoundaryChunker(),pcm(4)+pcm(.5,0))
    assert [(r.probe,r.forced,len(r.pcm)) for r in requests]==[(True,True,128000),(False,False,144000)]


def test_long_speech_remains_bounded_and_all_audio_is_covered():
    chunker=BoundaryChunker()
    data=pcm(22)+pcm(.5,0)
    requests=feed(chunker,data)
    finals=[r for r in requests if not r.probe]
    assert all(len(r.pcm)<=160000 for r in requests)
    assert all(b.start_sample<=a.end_sample for a,b in zip(finals,finals[1:]))
    assert finals[0].start_sample==0 and finals[-1].end_sample==len(data)//2
    assert chunker.buffered_seconds < 1


def test_empty_recheck_can_roll_back_consumption_without_losing_extra_speech():
    chunker=BoundaryChunker()
    feed(chunker,pcm(4))  # probe resolved
    extension=pcm(1,2000)
    chunker.feed(extension)
    request=chunker.next_request()
    assert request.rechecked
    chunker.resolve(request,use_extension=False)
    assert chunker.next_request() is None  # replays the complete extension
    requests=feed(chunker,pcm(.5,0))
    assert len(requests)==1
    final=requests[0]
    assert final.start_sample==60800
    assert final.pcm==pcm(.2)+extension+pcm(.5,0)
    assert final.end_sample==88000


def test_subframe_packets_and_resolve_contract():
    chunker=BoundaryChunker()
    chunker.feed(pcm(.4)+pcm(.49,0))
    assert chunker.next_request() is None
    chunker.feed(pcm(.01,0))
    request=chunker.next_request()
    assert request and not request.probe
    with pytest.raises(RuntimeError):chunker.next_request()
    with pytest.raises(RuntimeError):chunker.resolve(None)
    chunker.resolve(request)
    with pytest.raises(RuntimeError):chunker.resolve(request)


def test_maximum_packet_after_undrainable_partial_frame_is_valid():
    chunker=BoundaryChunker()
    chunker.feed(pcm(1/16000))
    assert chunker.next_request() is None
    chunker.feed(pcm(1))
    assert chunker.next_request() is None
    assert chunker.buffered_seconds == 1 + 1/16000


class Socket:
    def __init__(self):self.events=[]
    async def send_json(self,event):self.events.append(event)
    async def wait(self,predicate):
        async def poll():
            while not predicate(self.events):await asyncio.sleep(.005)
        await asyncio.wait_for(poll(),3)


class Runtime:
    asr_backend='qwen3_asr'
    translation_warnings=[]
    def __init__(self,responses):
        self.responses=deque(responses)
        self.calls=[]
        self.translations=[]
    async def prepare(self,settings):pass
    def transcribe(self,data,language):
        self.calls.append(data)
        result=self.responses.popleft()
        if isinstance(result,Exception):raise result
        return result,language
    async def translate(self,text,language,context=None, *, target_language="ko"):
        self.translations.append((text,list(context or [])))
        return '한국어 '+text


async def send(session,seconds,amplitude=1000):
    data=pcm(seconds,amplitude)
    for offset in range(0,len(data),3200):
        await session.feed(data[offset:offset+3200])
        await asyncio.sleep(0)


def session_for(runtime,enabled=True,language='zh'):
    socket=Socket()
    settings=SimpleNamespace(data={'qwen_boundary_recheck':enabled})
    return StreamSession(socket,runtime,SessionManager(),settings,'local',language),socket


@pytest.mark.asyncio
@pytest.mark.parametrize('language,first,second',[
    ('zh','但我唯一。','但我唯一知道的是，你不配。'),
    ('en','I have not.','I have not finished yet.'),
    ('ja','私はまだ。','私はまだ終わっていません。')])
async def test_unfinished_probe_is_never_translated_or_added_to_context(language,first,second):
    runtime=Runtime([first,second])
    session,socket=session_for(runtime,language=language)
    await session.start()
    try:
        await send(session,4)
        await socket.wait(lambda events:any(e['type']=='transcript' for e in events))
        assert runtime.translations==[]
        assert not any(e['type']=='caption' for e in socket.events)
        await send(session,1)
        await socket.wait(lambda events:any(e['type']=='caption' for e in events))
        assert runtime.translations==[(second,[])]
        assert len(runtime.calls)==2 and runtime.calls[1].startswith(runtime.calls[0])
    finally:await session.stop()


@pytest.mark.asyncio
async def test_disabled_option_uses_original_four_second_path():
    runtime=Runtime(['Old path.'])
    session,socket=session_for(runtime,enabled=False,language='en')
    await session.start()
    try:
        await send(session,4)
        await socket.wait(lambda events:any(e['type']=='caption' for e in events))
        assert len(runtime.calls)==1 and len(runtime.calls[0])==128000
    finally:await session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed',['',EngineError('asr_truncated','test',422)])
async def test_failed_recheck_keeps_the_first_result_and_reprocesses_extension(failed):
    runtime=Runtime(['First.',failed,'Next.'])
    session,socket=session_for(runtime,language='en')
    await session.start()
    try:
        await send(session,4)
        await socket.wait(lambda events:any(e['type']=='transcript' for e in events))
        await send(session,1,2000)
        await socket.wait(lambda events:any(e['type']=='caption' for e in events))
        await send(session,.5,0)
        await socket.wait(lambda events:sum(e['type']=='caption' for e in events)==2)
        assert [t[0] for t in runtime.translations]==['First.','Next.']
        assert runtime.calls[2]==pcm(.2)+pcm(1,2000)+pcm(.5,0)
        assert any(e.get('code')=='qwen_boundary_fallback' for e in socket.events)
        assert not any(e['type']=='error' for e in socket.events)
    finally:await session.stop()


@pytest.mark.asyncio
async def test_identical_speech_across_natural_pauses_remains_two_captions():
    runtime=Runtime(['ありがとう。','ありがとう。'])
    session,socket=session_for(runtime,language='ja')
    await session.start()
    try:
        for count in (1,2):
            await send(session,.3)
            await send(session,.5,0)
            await socket.wait(lambda events:sum(e['type']=='caption' for e in events)==count)
        captions=[e for e in socket.events if e['type']=='caption']
        assert [e['source_text'] for e in captions]==['ありがとう。','ありがとう。']
        assert len({e['segment_id'] for e in captions})==2
    finally:await session.stop()


@pytest.mark.asyncio
async def test_user_stop_does_not_finalize_pending_probe():
    runtime=Runtime(['Unfinished.'])
    session,socket=session_for(runtime,language='en')
    await session.start()
    await send(session,4)
    await socket.wait(lambda events:any(e['type']=='transcript' for e in events))
    await session.stop()
    assert not runtime.translations
    assert not any(e['type']=='caption' for e in socket.events)
