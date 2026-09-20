# Flowork Workflow Engine

The Engine is Flowork's framework-independent Python runtime for validating
and executing declarative workflows. It resolves a workflow definition into a
graph of typed nodes, manages data flow and control flow, and emits structured
events throughout execution.

The installable package and Python import namespace remain
`vibecanvas-engine` and `vibecanvas_engine` for compatibility. This document is
intended for contributors working on the Engine package; begin with the
repository [README](../README.md) for a product-level overview.

## Responsibilities and boundaries

The Engine owns:

- the in-memory Workflow model and node registry;
- structural and node-level validation;
- input references, output scopes, branching, loops, and parallel execution;
- asynchronous execution events, cancellation, and timeouts;
- built-in workflow nodes and provider-neutral model interfaces; and
- the entry point used to run the Engine inside an isolated environment.

It deliberately does not own HTTP APIs, authentication, tenancy, persistence,
deployment orchestration, or the Web interface. Those capabilities belong to
the [`api/`](../api/) and [`web/`](../web/) packages.

The Engine also does not create the platform's operating-system sandbox. In the
supported Flowork topology, the API-side `sandboxd` service starts gVisor and
runs the Engine inside it. The distinction is described in
[Execution isolation](#execution-isolation) below and in the repository
[architecture guide](../docs/architecture.md).

## Workflow model

A workflow is represented as a Python dictionary or equivalent JSON document:

- `__meta__` contains workflow identity and execution settings;
- every other top-level entry describes one node;
- `node_type` selects the registered node implementation;
- `input_fields` and `output_fields` define the node contract;
- `node_config` contains behavior specific to that node type;
- `children` defines outgoing graph edges; and
- input references such as `normalize.result` connect a downstream field to an
  upstream node output.

The graph has exactly one `StartNode`. Ordinary `children` edges form a directed
acyclic graph; loops and parallel branches use explicit paired control nodes.
The canonical test fixture provides a complete
[workflow definition](tests/example_workflow.json).

## Validation and execution

[`Workflow`](src/vibecanvas_engine/workflow.py) is the primary public entry
point. Validate a loaded definition before constructing or executing it:

```python
from vibecanvas_engine import Workflow

validation = Workflow.check(definition)
if validation["status"] != "success":
    raise ValueError(validation["error_message"])

workflow = Workflow(definition)
```

`Workflow.check()` returns a structured result instead of raising for an
invalid document. A successful result may also contain non-blocking `warnings`
for definitions that are executable but should be cleaned up.

Async callers should consume the execution event stream:

```python
async for event in workflow.astream(inputs):
    handle(event)
```

The stream reports node progress and ends with a `finished` event containing
`final_outputs`, `error_dict`, and `execution_time`. Cancellation propagates
through the run and active CodeNode workers are terminated during cleanup.

Synchronous callers may use the compatibility wrapper:

```python
outputs, errors, duration = workflow.trigger(inputs)
```

`trigger()` creates its own event loop and therefore cannot be called from an
already running async loop. New asynchronous integrations should use
`astream()` directly.

## Built-in nodes

Built-in node classes are registered when
[`nodes/`](src/vibecanvas_engine/nodes/) is imported.

| Category | Nodes | Purpose |
| --- | --- | --- |
| Graph boundaries | `StartNode`, `EndNode` | Define workflow inputs and terminal outputs |
| Control flow | `ConditionNode`, `ParallelStartNode`, `ParallelEndNode`, `LoopBeginNode`, `LoopEndNode` | Route, fan out, join, and repeat execution |
| Data processing | `CodeNode`, `TransformNode`, `TemplateNode` | Compute, transform, and format values |
| External interaction | `HTTPRequestNode`, `TableReadNode`, `TableWriteNode` | Exchange data through controlled runtime interfaces |
| Model execution | `PromptNode`, `SubAgentNode` | Invoke configured model or sub-agent capabilities |

The registry in [`register.py`](src/vibecanvas_engine/register.py) maps stable
`node_type` names to implementations without dynamic evaluation. Custom node
classes can derive from
[`BaseNode`](src/vibecanvas_engine/nodes/base.py) and register with
`node_registry`, but registration alone does not authorize a node for the
Flowork sandbox. Sandbox admission accepts only the Engine-native node set and
must remain an explicit host-side policy decision.

## Model interfaces

[`BaseLLM`](src/vibecanvas_engine/register.py) and `llm_registry` define the
provider-neutral model interface. Concrete provider adapters are implemented in
[`custom_llms.py`](src/vibecanvas_engine/custom_llms.py), while `PromptNode`
resolves the model selected in the workflow.

In the Flowork application, the API validates the user's saved model selection
and injects a short-lived runtime capability. The Engine consumes that
execution-scoped mapping; it does not persist provider credentials or choose a
tenant's model configuration. Inline provider secrets in workflow documents
are not supported.

## Execution isolation

Engine execution mechanisms should not be confused with the platform security
boundary:

- `CodeNode` uses a bounded subprocess pool for concurrency, timeouts, and
  process cleanup. A subprocess alone is not an isolation boundary.
- [`PythonSandbox`](src/vibecanvas_engine/sandbox.py) is a restricted expression
  evaluator used by Condition and Transform nodes. It is not a general-purpose
  sandbox for untrusted code.
- [`sandbox_entry.py`](src/vibecanvas_engine/sandbox_entry.py) is the
  credential-free Engine entry point executed inside the platform sandbox.
- gVisor lifecycle, filesystem bindings, network policy, and host capabilities
  are owned by the API-side
  [sandbox service](../api/src/vibecanvas_api/services/sandbox/).

Do not execute untrusted workflow definitions directly in a host Python
process. Use the complete Flowork runtime topology described in
[Sandbox lifecycle](../docs/architecture.md#sandbox-lifecycle).

## Development

Prepare the repository environment using the
[development guide](../docs/development.md), then run the Engine checks from the
repository root:

```bash
source .venv/bin/activate
python -m pytest -q engine/tests
ruff check engine/src
```

Changes must preserve the package boundary: the API may depend on the Engine,
but the Engine must not import FastAPI, SQLAlchemy, or other application-layer
modules. Dependency and contribution policies are documented in
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Source map

| Path | Responsibility |
| --- | --- |
| [`workflow.py`](src/vibecanvas_engine/workflow.py) | Workflow validation, orchestration, event streaming, and synchronous compatibility wrapper |
| [`nodes/`](src/vibecanvas_engine/nodes/) | Built-in node contracts and implementations |
| [`register.py`](src/vibecanvas_engine/register.py) | Node and model registries |
| [`custom_llms.py`](src/vibecanvas_engine/custom_llms.py) | Provider-neutral model adapters |
| [`code_runner.py`](src/vibecanvas_engine/code_runner.py) | Per-run CodeNode worker pool |
| [`sandbox_entry.py`](src/vibecanvas_engine/sandbox_entry.py) | Engine process entry point used inside the platform sandbox |
| [`sandbox_bus.py`](src/vibecanvas_engine/sandbox_bus.py) | Credential-free message channel to host-side brokers |
| [`tests/`](tests/) | Workflow, node, execution, and package-contract tests |

## License

Apache-2.0. See [`LICENSE`](../LICENSE).
