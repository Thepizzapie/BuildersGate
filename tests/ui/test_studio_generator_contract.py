"""Studio's shipped graph surface contains and runs generator nodes only."""
from __future__ import annotations

import re
from pathlib import Path

from bgate_core.board import wfnodes


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "frontend" / "public"


def source(name: str) -> str:
    return (PUBLIC / name).read_text(encoding="utf-8")


def test_studio_loads_only_the_generator_step_registries():
    scripts = set(re.findall(r'src="/static/(wf_steps_[^"]+\.js)"',
                             source("index.html")))
    assert scripts == {
        "wf_steps_model.js",
        "wf_steps_tools.js",
        "wf_steps_templates.js",
    }


def test_loaded_step_registries_cannot_define_agent_or_queue_work():
    loaded = "\n".join(source(name) for name in (
        "wf_steps_model.js", "wf_steps_tools.js", "wf_steps_templates.js"))
    assert "agentSeat" not in loaded
    assert not re.search(r'kind\s*:\s*["\']agent["\']', loaded)
    assert "/api/queue" not in loaded


def test_every_shipped_template_node_has_a_live_executor():
    core = source("wf.js")
    model = source("wf_steps_model.js")
    templates = model + "\n" + source("wf_steps_templates.js")

    browser_steps = set(re.findall(
        r'registerStep\s*\(\s*\{.*?\btype\s*:\s*"([^"]+)"',
        core + "\n" + model, flags=re.DOTALL))
    template_types = set(re.findall(
        r'\{\s*id\s*:\s*"[^"]+"\s*,\s*type\s*:\s*"([^"]+)"',
        templates, flags=re.DOTALL))
    executable = browser_steps | set(wfnodes.REGISTRY) | set(wfnodes.FLOW_TYPES)

    assert template_types
    assert template_types <= executable, sorted(template_types - executable)


def test_whole_graph_run_executes_generators_without_agent_dispatch():
    client = source("wf.js")
    route = (ROOT / "src" / "bgate_ui" / "routes" / "workflows.py").read_text(
        encoding="utf-8")
    assert 'mode: "generator", dispatch: true' in client
    assert "agent_nodes" in route
    assert "dispatch = (True if generator_mode else" in route
