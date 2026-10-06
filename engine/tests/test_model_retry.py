import asyncio
import pytest
from vibecanvas_engine.model_retry import acall_with_retry, call_with_retry, EmptyModelResponse, observations


def test_zero_means_one_attempt():
    calls=[]
    def call():
        calls.append(1)
        raise EmptyModelResponse('empty')
    with pytest.raises(EmptyModelResponse):call_with_retry(call, 0)
    assert len(calls)==1


def test_permanent_error_not_retried():
    calls=[]
    def call():
        calls.append(1)
        raise ValueError('invalid configuration')
    with pytest.raises(ValueError):call_with_retry(call, 3)
    assert len(calls)==1


@pytest.mark.asyncio
async def test_retry_recovers_and_records_first_failure():
    calls=[]
    async def call():
        calls.append(1)
        if len(calls)==1:raise EmptyModelResponse('empty')
        return 'ok'
    records=[]; token=observations.set(records)
    try: assert await acall_with_retry(call, 1)=='ok'
    finally: observations.reset(token)
    assert records==[{'attempts':2,'failures':[{'attempt':1,'error_type':'EmptyModelResponse'}],'succeeded':True}]


@pytest.mark.asyncio
async def test_cancel_during_backoff_does_not_retry():
    stop=asyncio.Event();calls=[]
    async def call():
        calls.append(1)
        stop.set()
        raise EmptyModelResponse('empty')
    with pytest.raises(asyncio.CancelledError):await acall_with_retry(call,3,stop)
    assert len(calls)==1


@pytest.mark.parametrize('retry',[-1,True,1.2,11])
def test_invalid_retry(retry):
    with pytest.raises(ValueError):call_with_retry(lambda:'ok',retry)


def test_length_failure_records_usage_without_response_content():
    from types import SimpleNamespace as NS
    from vibecanvas_engine.custom_llms import _openai_completion_text
    response = NS(choices=[NS(finish_reason='length', message=NS(content=''))],
                  usage=NS(prompt_tokens=91, completion_tokens=2048, total_tokens=2139,
                           completion_tokens_details=NS(reasoning_tokens=2048)),
                  secret='never record', id='private-provider-id')
    records=[];token=observations.set(records)
    try:
        with pytest.raises(RuntimeError, match='finish_reason=length'):
            call_with_retry(lambda: _openai_completion_text(response),3)
    finally:observations.reset(token)
    assert records[0]['attempts']==1
    assert records[0]['responses']==[{'attempt':1,'finish_reason':'length','usage':{
        'prompt_tokens':91,'completion_tokens':2048,'total_tokens':2139,'reasoning_tokens':2048}}]
    assert 'never record' not in str(records) and 'private-provider-id' not in str(records)


@pytest.mark.asyncio
async def test_concurrent_response_metadata_stays_with_its_call():
    from vibecanvas_engine.model_retry import record_openai_response
    records=[];token=observations.set(records)
    async def call(count):
        await asyncio.sleep(0)
        record_openai_response({'choices':[{'finish_reason':'stop'}],
                                'usage':{'completion_tokens':count,'prompt_tokens':'secret'}})
        return count
    try:
        assert await asyncio.gather(acall_with_retry(lambda:call(3)),
                                    acall_with_retry(lambda:call(7)))==[3,7]
    finally:observations.reset(token)
    assert [r['responses'][0]['usage'] for r in records]==[
        {'completion_tokens':3},{'completion_tokens':7}]


def test_empty_length_error_is_actionable_even_without_observation_collector():
    from types import SimpleNamespace as NS
    from vibecanvas_engine.custom_llms import _openai_completion_text
    response = NS(choices=[NS(finish_reason='length', message=NS(content=None))],
                  usage=NS(completion_tokens=2048, completion_tokens_details=NS(reasoning_tokens=2048)))
    with pytest.raises(RuntimeError) as error:
        _openai_completion_text(response)
    message = str(error.value)
    for expected in ('finish_reason=length', 'completion_tokens=2048', 'reasoning_tokens=2048',
                     'inference_config.max_tokens', 'reduce/disable reasoning', 'retry alone'):
        assert expected in message


def test_length_with_text_is_preserved_and_missing_usage_is_not_invented():
    from types import SimpleNamespace as NS
    from vibecanvas_engine.custom_llms import _openai_completion_text
    response = NS(choices=[NS(finish_reason='length', message=NS(content='{"answer": 3}'))])
    assert _openai_completion_text(response) == '{"answer": 3}'
    response.choices[0].message.content = ''
    with pytest.raises(RuntimeError, match='Token usage: not reported by provider'):
        _openai_completion_text(response)
