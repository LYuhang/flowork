"""Retries reuse successful visits, preserving loops, branches and approvals."""
import asyncio
from collections import Counter
from copy import deepcopy
import importlib

import pytest

from vibecanvas_engine.workflow import Workflow
from vibecanvas_engine.resume import successful_visits
from vibecanvas_engine.nodes.loop import LoopBeginNode, LoopEndNode
from vibecanvas_engine.nodes.parallel import ParallelStartNode, ParallelEndNode
from test_human_approval_runtime import approval_workflow


def graph(mode):
    g = approval_workflow()
    # A completed approval is a side effect too: it must not be requested again.
    g['node_4'] = deepcopy(g['node_2'])
    g['node_4'].update(node_id='node_4', node_name='second', children=['node_3'])
    g['node_2']['children'] = ['node_4']
    if mode == 'loop':
        begin = deepcopy(LoopBeginNode.AGENT_SPEC['examples'][0]['node_dict'])
        end = deepcopy(LoopEndNode.AGENT_SPEC['examples'][0]['node_dict'])
        begin.update(node_id='node_5', children=['node_2'])
        begin['node_config'].update(loop_end_node_id='node_6', end_value={'value':3,'reference':''})
        end.update(node_id='node_6', children=['node_3'])
        end['node_config'] = {'loop_begin_node_id':'node_5'}
        g['node_1']['children'] = ['node_5']
        g['node_4']['children'] = ['node_6']
        g.update(node_5=begin,node_6=end)
        g['node_3']['input_fields'] = {'iterations': {'type':'array','value':[], 'reference':begin['node_name']+'.loop_output'}}
        g['node_3']['output_fields'] = {'iterations': {'type':'array','description':'outputs'}}
    elif mode == 'parallel':
        begin = deepcopy(ParallelStartNode.AGENT_SPEC['examples'][0]['node_dict'])
        end = deepcopy(ParallelEndNode.AGENT_SPEC['examples'][0]['node_dict'])
        begin.update(node_id='node_5', children=['node_2','node_4'])
        begin['node_config'] = {'parallel_end_node_id':'node_6', 'branches':{'a':{'next_node_id':'node_2'},'b':{'next_node_id':'node_4'}}}
        end.update(node_id='node_6',children=['node_3'],input_fields={},output_fields={})
        end['node_config'] = {'parallel_start_node_id':'node_5'}
        g['node_1']['children']=['node_5']
        g['node_2']['children']=g['node_4']['children']=['node_6']
        g.update(node_5=begin,node_6=end)
    assert Workflow.check(g)['status']=='success'
    return g


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['linear','loop','parallel'])
async def test_repeated_retry_skips_successful_visits_not_whole_loop_nodes(monkeypatch, mode):
    module=importlib.import_module('vibecanvas_engine.nodes.trigger')
    original=module.dispatch_node_call
    counts=Counter()
    attempt=0
    async def dispatch(node, inputs, previous, *, extra):
        if node.node_type != 'HumanApprovalNode':
            return await original(node,inputs,previous,extra=extra)
        stack=extra['loop_signal']['loop_stack']
        iteration=stack[-1]['iter_index'] if stack else 0
        counts[node.node_id,iteration]+=1
        if node.node_id=='node_4' and iteration==(1 if mode=='loop' else 0) and attempt<2:
            await asyncio.sleep(.01) # the other parallel branch finishes first
            return {'status':'error','error_message':'temporary failure','traceback':'','output':None}
        return {'status':'success','output':{'approved':True},'error_message':''}
    monkeypatch.setattr(module,'dispatch_node_call',dispatch)
    cache={}
    for attempt in range(3):
        events=[e async for e in Workflow(graph(mode)).astream({},run_context={'workflow_resume_visits':cache})]
        result=events[-1]
        assert bool(result['error_dict']) is (attempt<2)
        cache=successful_visits([{**e,'type':'node_event'} for e in events])
        if attempt:
            assert any(e.get('reused') for e in events)
    assert counts['node_4',1 if mode=='loop' else 0]==3
    assert counts['node_2',0]==1
    if mode=='loop':
        assert counts['node_2',1]==counts['node_2',2]==counts['node_4',0]==counts['node_4',2]==1
        assert len(result['final_outputs']['__end__']['iterations'])==3
