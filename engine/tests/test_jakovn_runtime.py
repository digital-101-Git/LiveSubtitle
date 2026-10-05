"""ja-ko-vn-12b-v2 prompt/transport regression; no model, GPU or network calls."""
import copy
import io
import json
import os
from pathlib import Path

import httpx
import pytest

from engine.runtime import Runtime, korean_output, target_output, translation_envelope
from engine.settings import EngineError
from engine.translation_profiles import JAKOVN_STOP, build_jakovn_prompt, is_jakovn_model


MODEL = 'ja-ko-vn-12b-v2-Q4_K_M.gguf'
SYSTEM = '당신은 전문 일한 번역가입니다. 주어진 일본어를 한국어로 번역하세요.'


@pytest.mark.parametrize('path,expected',[
    (MODEL,True), ('JA_KO_VN_12B_V2.Q8_0.gguf',True),
    ('author-ja-ko-vn-12b-v2-Q4_K_M.gguf',True),
    (Path('models/translation')/MODEL,True),
    (r'D:\models\ja-ko-vn-12b-v2-Q4_K_M.gguf',True),
    (None,False), ('ja-ko-vn-12b-v1-Q4_K_M.gguf',False),
    ('ja-ko-vn-7b-v2-Q4_K_M.gguf',False), ('ja-ko-vn-12b-v20.gguf',False),
    ('ja-ko-vn-12b-v2x.gguf',False), ('notja-ko-vn-12b-v2.gguf',False),
    ('models/ja-ko-vn-12b-v2/Qwen3.5-9B.gguf',False),
    (r'D:\ja-ko-vn-12b-v2\other.gguf',False),
    ('ja-ko-vn-12b-Q4_K_M.gguf',False), ('TranslateGemma-12B-Q4_K_M.gguf',False),
])
def test_profile_selected_by_bounded_v2_filename_only(path,expected):
    assert is_jakovn_model(path) is expected


def test_exact_embedded_template_and_single_generation_turn():
    source='次の試合が始まります。'
    expected=(f'<start_of_turn>system\n{SYSTEM}<end_of_turn>\n'
              f'<start_of_turn>user\n{source}<end_of_turn>\n'
              '<start_of_turn>model\n')
    assert build_jakovn_prompt(' \n'+source+'\n ')==expected
    assert '<bos>' not in expected and expected.count('<start_of_turn>')==3
    assert expected.count('<end_of_turn>')==2
    assert JAKOVN_STOP==('<end_of_turn>','<eos>')


def test_optional_manual_bos_is_exactly_one_prefix():
    prompt=build_jakovn_prompt('こんにちは。')
    assert build_jakovn_prompt('こんにちは。',include_bos=True)=='<bos>'+prompt


def test_prompt_escapes_source_control_tokens_but_preserves_source_json_and_comparisons():
    source='文字 <eos><bos><start_of_turn><end_of_turn><|extra_0|> と x < 3。\n{"message":"こんにちは"}'
    prompt=build_jakovn_prompt(source)
    assert prompt.count('<start_of_turn>')==3 and prompt.count('<end_of_turn>')==2
    assert '<bos>' not in prompt and '<eos>' not in prompt and '<|extra_0|>' not in prompt
    assert '＜eos＞＜bos＞＜start_of_turn＞＜end_of_turn＞＜|extra_0|＞' in prompt
    assert 'x < 3。\n{"message":"こんにちは"}' in prompt


@pytest.mark.parametrize('source,error',[('',ValueError),(' \n\t',ValueError),(None,TypeError),(3,TypeError)])
def test_invalid_prompt_input_rejected(source,error):
    with pytest.raises(error):
        build_jakovn_prompt(source)


def mock_runtime(tmp_path,monkeypatch,replies,model=MODEL):
    calls=[]
    remaining=list(replies)
    class Client:
        async def post(self,url,headers,json):
            calls.append({'url':url,'payload':copy.deepcopy(json)})
            assert remaining, 'Unexpected repeated HTTP request'
            reply=remaining.pop(0)
            if isinstance(reply,BaseException):
                raise reply
            if isinstance(reply,httpx.Response):
                return reply
            return httpx.Response(200,json=reply,request=httpx.Request('POST',url))
    runtime=Runtime(tmp_path)
    runtime.translation_path=tmp_path/'models/translation'/model
    runtime.llama_url='http://127.0.0.1:1'
    monkeypatch.setattr(runtime,'status',lambda:{'translation_ready':True})
    monkeypatch.setattr(runtime,'_get_translation_client',lambda:Client())
    return runtime,calls


@pytest.mark.asyncio
@pytest.mark.parametrize('source,language',[
    ('次の試合が始まります。','ja'),('東京','ja-JP'),
    ('東京','auto'),('こんにちは。','auto'),
])
async def test_source_only_completion_with_exact_greedy_sampler(tmp_path,monkeypatch,source,language):
    runtime,calls=mock_runtime(tmp_path,monkeypatch,[{'content':' 곧 경기가 시작됩니다. '}])
    runtime.translation_glossary=[{'source':source,'target':'GLOSSARY_SENTINEL'}]
    assert await runtime.translate(source,language,['CONTEXT_SENTINEL'])=='곧 경기가 시작됩니다.'
    assert len(calls)==1 and calls[0]['url'].endswith('/completion')
    payload=calls[0]['payload']
    assert payload=={
        'prompt':build_jakovn_prompt(source),'n_predict':320,'stream':False,
        'temperature':0,'top_k':1,'top_p':1,'min_p':0,'repeat_penalty':1.05,
        'stop':list(JAKOVN_STOP),'cache_prompt':True,
    }
    assert 'CONTEXT_SENTINEL' not in payload['prompt'] and 'GLOSSARY_SENTINEL' not in payload['prompt']
    assert 'messages' not in payload and 'chat_template_kwargs' not in payload
    assert '"current_text"' not in payload['prompt'] and '"source_language"' not in payload['prompt']


@pytest.mark.asyncio
@pytest.mark.parametrize('language',['ko','ko-KR'])
async def test_same_korean_source_returns_without_request(tmp_path,monkeypatch,language):
    runtime,calls=mock_runtime(tmp_path,monkeypatch,[])
    assert await runtime.translate('안녕하세요.',language,target_language='ko')=='안녕하세요.'
    assert calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('source_language,target',[
    ('en','ko'),('zh','ko'),('zh-Hant','ko'),('de','ko'),
    ('ja','en'),('ja','zh'),('auto','en'),('ko','en'),
])
async def test_unsupported_translation_pair_fails_before_http(tmp_path,monkeypatch,source_language,target):
    runtime,calls=mock_runtime(tmp_path,monkeypatch,[])
    with pytest.raises(EngineError) as caught:
        await runtime.translate('発話です。',source_language,target_language=target)
    assert caught.value.code=='translation_language' and caught.value.status==422
    assert calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('content,code',[
    ('','translation_empty'),(' ... ','translation_empty'),('번역: ...','translation_empty'),
    ('<think>추론입니다.</think>','translation_empty'),
    ('こんにちは。','translation_language'),('Hello.','translation_language'),
    ('안녕你好','translation_language'),
])
async def test_bad_output_is_one_recoverable_attempt(tmp_path,monkeypatch,content,code):
    runtime,calls=mock_runtime(tmp_path,monkeypatch,[{'content':content}])
    with pytest.raises(EngineError) as caught:
        await runtime.translate('こんにちは。','ja')
    assert caught.value.code==code and caught.value.status==422
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('flags',[{'stop_type':'limit'},{'stopped_limit':True},{'truncated':True}])
async def test_truncation_is_not_published_or_retried(tmp_path,monkeypatch,flags):
    runtime,calls=mock_runtime(tmp_path,monkeypatch,[{'content':'잘린 번역',**flags}])
    with pytest.raises(EngineError) as caught:
        await runtime.translate('長い文です。','ja')
    assert caught.value.code=='translation_truncated' and caught.value.status==422
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('reply',[
    {},{'content':None},{'content':[]},{'content':{'text':'잘못된 구조'}},[],None,
    httpx.Response(503,request=httpx.Request('POST','http://127.0.0.1:1/completion')),
    httpx.ConnectError('synthetic unavailable'),
])
async def test_transport_and_schema_failure_is_distinct(tmp_path,monkeypatch,reply):
    runtime,calls=mock_runtime(tmp_path,monkeypatch,[reply])
    with pytest.raises(EngineError) as caught:
        await runtime.translate('こんにちは。','ja')
    assert caught.value.code=='translation_failed' and caught.value.status==503
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('source,target',[('en','ko'),('zh','ko'),('ko','ko'),('ja','en'),('auto','zh')])
async def test_prepare_rejects_unsupported_pair_without_disturbing_models(tmp_path,monkeypatch,source,target):
    import engine.runtime as module
    runtime=Runtime(tmp_path)
    previous_model=object()
    runtime.asr=previous_model
    old_translation=tmp_path/'previous.gguf'
    runtime.translation_path=old_translation
    mutations=[]
    async def start(path):
        mutations.append('start_llama')
    monkeypatch.setattr(module,'confined_model',lambda root,path,kind:root/path)
    monkeypatch.setattr(module,'validate_gguf',lambda path:None)
    monkeypatch.setattr(runtime,'_start_llama',start)
    monkeypatch.setattr(runtime,'_unload_asr',lambda:mutations.append('unload_asr'))
    monkeypatch.setattr(runtime,'_load_asr',lambda path:mutations.append('load_asr'))
    with pytest.raises(EngineError) as caught:
        await runtime.prepare({'mode':'gemini','language':source,'target_language':target,
                               'translation_model':'models/translation/'+MODEL})
    assert caught.value.code=='translation_pair_unsupported'
    assert mutations==[] and runtime.asr is previous_model and runtime.translation_path==old_translation


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['auto','ja'])
async def test_prepare_accepts_supported_pair(tmp_path,monkeypatch,source):
    import engine.runtime as module
    runtime=Runtime(tmp_path)
    started=[]
    async def start(path):
        started.append(path)
    monkeypatch.setattr(module,'confined_model',lambda root,path,kind:root/path)
    monkeypatch.setattr(module,'validate_gguf',lambda path:None)
    monkeypatch.setattr(runtime,'_start_llama',start)
    monkeypatch.setattr(runtime,'_unload_asr',lambda:None)
    await runtime.prepare({'mode':'gemini','language':source,'target_language':'ko',
                           'translation_model':'models/translation/'+MODEL})
    assert runtime.state=='ready' and len(started)==1


@pytest.mark.asyncio
async def test_startup_uses_embedded_jinja_and_small_ubatch_without_thinking_flag(tmp_path,monkeypatch):
    import engine.runtime as module
    binary=tmp_path/'runtime/llama'/('llama-server.exe' if os.name=='nt' else 'llama-server')
    binary.parent.mkdir(parents=True)
    binary.touch()
    started=[]
    class Process:
        def __init__(self,args,**kwargs):
            started.append(args)
            self.stdout=io.BytesIO(b'')
            self.closed=False
        def poll(self):return 0 if self.closed else None
        def terminate(self):self.closed=True
        def wait(self,timeout):return 0
    class Client:
        async def get(self,*args,**kwargs):return httpx.Response(200)
        async def aclose(self):pass
    monkeypatch.setattr(module.subprocess,'Popen',Process)
    runtime=Runtime(tmp_path)
    monkeypatch.setattr(runtime,'_get_translation_client',lambda:Client())
    try:
        await runtime._start_llama(tmp_path/MODEL)
        args=started[0]
        assert '--jinja' in args and args[args.index('--ubatch-size')+1]=='128'
        assert '--chat-template-kwargs' not in args and '--chat-template' not in args
    finally:
        await runtime._stop_llama()


@pytest.mark.parametrize('target,text',[
    ('ko','안녕하세요.'),('en','Hello.'),('zh','你好。'),('ja','こんにちは。'),
])
@pytest.mark.parametrize('fenced',[False,True])
def test_transport_envelope_is_rejected_in_every_output_language(target,text,fenced):
    content=json.dumps({'source_language':'ja','target_language':target,'context':[],
                        'current_text':text},ensure_ascii=False)
    if fenced:
        content='```json\n'+content+'\n```'
    assert translation_envelope(content)
    assert not target_output(content,target)
    if target=='ko':
        assert not korean_output(content)


@pytest.mark.parametrize('key,value',[('source_language','ja'),('target_language','ko'),('context',[])])
def test_one_transport_metadata_key_with_current_text_is_sufficient(key,value):
    assert translation_envelope(json.dumps({'current_text':'안녕',key:value},ensure_ascii=False))


@pytest.mark.parametrize('target,text',[
    ('ko','안녕하세요.'),('en','Hello.'),('zh','你好。'),('ja','こんにちは。'),
])
def test_legitimate_json_without_transport_contract_is_not_rejected(target,text):
    content=json.dumps({'message':text},ensure_ascii=False)
    assert not translation_envelope(content)
    assert target_output(content,target)


@pytest.mark.parametrize('content',[
    '{"current_text":"안녕"}', '{"source_language":"ja","message":"안녕"}',
    '설명: {"current_text":"안녕","context":[]}',
    '{"current_text":"안녕","context":[]',
])
def test_envelope_guard_is_narrow_and_does_not_extract_source(content):
    assert not translation_envelope(content)


@pytest.mark.asyncio
async def test_leaked_transport_json_is_failed_not_unwrapped_into_subtitle(tmp_path,monkeypatch):
    content=json.dumps({'current_text':'잘못된 추출을 표시하면 안 됩니다.','context':[]},ensure_ascii=False)
    runtime,calls=mock_runtime(tmp_path,monkeypatch,[{'content':content}])
    with pytest.raises(EngineError) as caught:
        await runtime.translate('こんにちは。','ja')
    assert caught.value.code=='translation_language' and caught.value.status==422
    assert len(calls)==1
