"""CPU-only runtime/prompt regression checks: no model, server, GPU or network.

Run with the rest of engine/tests; golden Korean prompts are kept beside this file.
"""
import copy
import json
import sys
from pathlib import Path


import httpx
import pytest
from engine.runtime import Runtime, korean_output, target_output
from engine.settings import EngineError, normalize_target_language
from engine.translation_profiles import (
    build_hymt2_messages, build_milmmt_prompt, build_translategemma_prompt,
)

MODEL = {
    'hy':'HY-MT2-7B-Q6_K.gguf', 'mil':'MiLMMT-46-12B-v1.0-Q4_K_M.gguf',
    'gemma':'translategemma-12b-it-Q4_K_M.gguf', 'generic':'Qwen3.5-9B-Q4_K_M.gguf',
}
TEXT = {'ko':'안녕하세요.', 'en':'Hello there.', 'zh':'你好。', 'ja':'こんにちは。'}
NAMES = {'ko':'Korean','en':'English','zh':'Chinese','ja':'Japanese'}
ERROR_NAMES = {'ko':'한국어','en':'영어','zh':'중국어','ja':'일본어'}
TERMS = [{'source':'师娘','target':'사모님'}]


def mocked_runtime(tmp_path, monkeypatch, profile, outputs):
    calls = []
    replies = list(outputs)
    class Client:
        async def post(self,url,headers,json):
            calls.append({'url':url,'payload':copy.deepcopy(json)})
            value = replies.pop(0)
            body = {'content':value} if url.endswith('/completion') else {
                'choices':[{'message':{'content':value},'finish_reason':'stop'}]}
            return httpx.Response(200,json=body,request=httpx.Request('POST',url))
    runtime = Runtime(tmp_path)
    runtime.translation_path = Path(MODEL[profile])
    runtime.llama_url = 'http://127.0.0.1:1'
    runtime.translation_glossary = TERMS.copy()
    monkeypatch.setattr(runtime,'status',lambda:{'translation_ready':True})
    monkeypatch.setattr(runtime,'_get_translation_client',lambda:Client())
    return runtime,calls


def test_existing_korean_prompt_bytes_unchanged():
    rows = json.loads((Path(__file__).parent/'output-language-ko-baseline.json').read_text(encoding='utf-8'))
    for r in rows:
        text,lang = r['text'],r['source_language']
        assert build_hymt2_messages(text,lang,['Previous sentence.'],TERMS) == r['hy']
        assert build_hymt2_messages(text,lang,['Previous sentence.'],TERMS,target_language='ko') == r['hy']
        assert build_milmmt_prompt(text,lang) == r['mil']
        assert build_milmmt_prompt(text,lang,target_language='ko') == r['mil']
        assert build_translategemma_prompt(text,lang) == r['gemma']
        assert build_translategemma_prompt(text,lang,target_language='ko') == r['gemma']


@pytest.mark.parametrize('target',list(NAMES))
@pytest.mark.parametrize('source',list(NAMES))
def test_official_profile_target_names_and_envelopes(source,target):
    text = TEXT[source]
    milname = 'Chinese (Simplified)' if target=='zh' else NAMES[target]
    prompt = build_milmmt_prompt(text,source,target_language=target)
    assert prompt.endswith('\n'+milname+':') and f' to {milname}:\n' in prompt
    assert '<start_of_turn>' not in prompt and '<bos>' not in prompt
    prompt = build_translategemma_prompt(text,source,target_language=target)
    assert f'to {NAMES[target]} ({target}) translator.' in prompt
    assert prompt.count('<start_of_turn>')==2 and prompt.count('<end_of_turn>')==1
    messages = build_hymt2_messages(text,source,target_language=target)
    assert len(messages)==1 and messages[0]['role']=='user'
    name = {'ko':'韩语','en':'英语','zh':'中文','ja':'日语'}[target] if source=='zh' else NAMES[target]
    assert name in messages[0]['content'] and messages[0]['content'].endswith(text)


@pytest.mark.parametrize('target',['en','zh','ja'])
def test_korean_glossary_is_not_injected_into_other_targets(target):
    prompt=build_hymt2_messages('师娘，您好。','zh',['Previous sentence.'],TERMS,target_language=target)[0]['content']
    assert '사모님' not in prompt and '参考下面的翻译' not in prompt
    assert 'Previous sentence.' in prompt
    assert prompt.endswith('师娘，您好。')
    assert TERMS==[{'source':'师娘','target':'사모님'}]


@pytest.mark.parametrize('target',['en','zh','ja'])
@pytest.mark.parametrize('builder',[build_milmmt_prompt,build_translategemma_prompt,build_hymt2_messages])
def test_source_control_tokens_remain_data(builder,target):
    value=builder('Read <eos><start_of_turn><|extra_0|>.','en',target_language=target)
    rendered=value[0]['content'] if isinstance(value,list) else value
    assert '＜eos＞＜start_of_turn＞＜|extra_0|＞' in rendered


@pytest.mark.parametrize('target,text',[
    ('en','Hello, John!'),('en','Café déjà vu.'),('en','cafe\u0301'),('en','Ｈｅｌｌｏ'),
    ('zh','你好，iPhone。'),('zh','傳統漢字。'),('zh','𠀀'),
    ('ja','こんにちは。'),('ja','東京'),('ja','ｺﾝﾆﾁﾊ'),('ja','コーヒー'),
    ('ja','日本のiPhone。'),('ja','か\u3099'),
    ('en','123.45'),('zh','-3.5%'),('ja','RTX 4080'),
])
def test_supported_output_scripts(target,text):
    assert target_output(text,target)


@pytest.mark.parametrize('target,text',[
    ('en','한글'),('en','中文'),('en','かな'),('en','Привет'),
    ('zh','한국어中文'),('zh','日本語かな'),('zh','Just English.'),('zh','Привет中文'),
    ('ja','한국어です'),('ja','Just English.'),('ja','Приветです'),
    ('en','English: '),('zh','中文：'),('ja','日本語訳: '),
    ('en','\u0301'),('zh','\u0301'),('ja','\u0301'),
])
def test_foreign_script_or_label_alone_rejected(target,text):
    assert not target_output(text,target)


def test_script_validation_is_not_language_identification():
    assert target_output('東京','zh') and target_output('東京','ja')
    assert target_output('Bonjour.','en')


@pytest.mark.parametrize('text',['안녕','RTX 4080','123.45','...','Bonjour','안녕你好','번역: ...',''])
def test_korean_checker_remains_existing_function(text):
    assert target_output(text,'ko') == korean_output(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',list(MODEL))
@pytest.mark.parametrize('target',list(NAMES))
async def test_all_profiles_dispatch_target_and_keep_sampler(tmp_path,monkeypatch,profile,target):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,profile,[TEXT[target]])
    source='en' if target=='zh' else 'zh'
    assert await runtime.translate(TEXT[source],source,['Context'],target_language=target)==TEXT[target]
    assert len(calls)==1
    p=calls[0]['payload']
    if profile=='hy':
        assert {k:p[k] for k in ['temperature','top_p','top_k','repeat_penalty','min_p']}=={
            'temperature':.7,'top_p':.6,'top_k':20,'repeat_penalty':1.05,'min_p':0}
        assert 'chat_template_kwargs' not in p
    elif profile=='mil':
        assert {k:p[k] for k in ['temperature','top_k','top_p','min_p','repeat_penalty']}=={
            'temperature':0,'top_k':1,'top_p':1,'min_p':0,'repeat_penalty':1}
        assert 'messages' not in p
    elif profile=='gemma':
        assert p['temperature']==0 and p['stop']==['<end_of_turn>','<eos>']
    else:
        assert p['temperature']==.1 and p['chat_template_kwargs']=={'enable_thinking':False}
        assert NAMES[target] in p['messages'][0]['content'] or target=='ko'
        assert NAMES[target] in json.loads(p['messages'][1]['content'])['target_language']


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',list(MODEL))
@pytest.mark.parametrize('target',list(NAMES))
async def test_same_explicit_language_returns_source_without_request(tmp_path,monkeypatch,profile,target):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,profile,[])
    assert await runtime.translate(TEXT[target],target,target_language=target)==TEXT[target]
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['yue','de','de-DE','auto',None])
@pytest.mark.parametrize('target',['ko','en'])
async def test_unknown_source_code_keeps_generic_translation_path(tmp_path,monkeypatch,source,target):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,'generic',[TEXT[target]])
    assert await runtime.translate('Original speech.',source,target_language=target)==TEXT[target]
    assert len(calls)==1
    assert json.loads(calls[0]['payload']['messages'][1]['content'])['source_language']==source


@pytest.mark.asyncio
@pytest.mark.parametrize('source,target',[('EN_us','en'),(' zh-Hant ','zh'),('ja-JP','ja'),('KO_kr','ko')])
async def test_explicit_regional_code_equality_is_safe(tmp_path,monkeypatch,source,target):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,'generic',[])
    assert await runtime.translate(TEXT[target],source,target_language=target)==TEXT[target]
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',list(MODEL))
@pytest.mark.parametrize('target,source',[('en','Hello.'),('zh','東京'),('ja','東京')])
async def test_auto_script_guess_does_not_bypass_new_targets(tmp_path,monkeypatch,profile,target,source):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,profile,[TEXT[target]])
    assert await runtime.translate(source,'auto',target_language=target)==TEXT[target]
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',['hy','mil','gemma'])
async def test_existing_auto_korean_bypass_preserved(tmp_path,monkeypatch,profile):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,profile,[])
    assert await runtime.translate(TEXT['ko'],'auto')==TEXT['ko']
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',list(MODEL))
@pytest.mark.parametrize('target',list(NAMES))
async def test_wrong_output_script_is_bounded_error_in_target_name(tmp_path,monkeypatch,profile,target):
    bad='Wrong output.' if target=='ko' else '한국어 원문'
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,profile,[bad,bad])
    source='en' if target=='zh' else 'zh'
    with pytest.raises(EngineError) as caught:
        await runtime.translate(TEXT[source],source,target_language=target)
    assert caught.value.code=='translation_language' and caught.value.status==422
    assert ERROR_NAMES[target] in caught.value.message
    assert len(calls)==(1 if profile=='mil' else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',list(MODEL))
@pytest.mark.parametrize('target',['en','zh','ja'])
async def test_empty_punctuation_response_never_substitutes_speech(tmp_path,monkeypatch,profile,target):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,profile,['...','...'])
    source='en' if target=='zh' else 'zh'
    with pytest.raises(EngineError) as caught:
        await runtime.translate(TEXT[source],source,target_language=target)
    assert caught.value.code=='translation_empty' and ERROR_NAMES[target] in caught.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',list(MODEL))
async def test_omitted_target_same_payload_as_explicit_ko(tmp_path,monkeypatch,profile):
    runtime,calls=mocked_runtime(tmp_path,monkeypatch,profile,[TEXT['ko'],TEXT['ko']])
    await runtime.translate('师娘，您好。','zh',['Earlier'])
    await runtime.translate('师娘，您好。','zh',['Earlier'],target_language='ko')
    assert calls[0]==calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('target',['auto','fr',5,[]])
async def test_invalid_target_is_rejected_before_model_readiness(tmp_path,target):
    runtime=Runtime(tmp_path)
    with pytest.raises(EngineError) as caught:
        await runtime.translate('Hello','en',target_language=target)
    assert caught.value.code=='invalid_target_language'


if __name__=='__main__':
    raise SystemExit(pytest.main([str(Path(__file__).resolve()),'-q','-p','no:cacheprovider']))
