import json
import subprocess
from bgate_ui import tailnet


def _runner_ok(*a, **k):
    return subprocess.CompletedProcess(a, 0, stdout="100.101.102.103\n", stderr="")


def _runner_fail(*a, **k):
    raise FileNotFoundError("tailscale not installed")


def test_is_tailnet_ip():
    assert tailnet.is_tailnet_ip("100.101.102.103")
    assert not tailnet.is_tailnet_ip("192.168.1.5")
    assert not tailnet.is_tailnet_ip("127.0.0.1")


def test_detect_via_cli():
    addr = tailnet.detect(runner=_runner_ok, interfaces=lambda: [])
    assert addr is not None and addr.ip == "100.101.102.103"


def test_detect_falls_back_to_interface_scan():
    addr = tailnet.detect(runner=_runner_fail,
                          interfaces=lambda: ["192.168.1.5", "100.64.0.9"])
    assert addr is not None and addr.ip == "100.64.0.9"


def test_detect_returns_none_when_no_tailnet():
    addr = tailnet.detect(runner=_runner_fail,
                          interfaces=lambda: ["192.168.1.5"])
    assert addr is None


def test_pairing_payload_is_json():
    s = tailnet.pairing_payload("http://100.64.0.9:7788", "abc123", "MyGame")
    obj = json.loads(s)
    assert obj == {"url": "http://100.64.0.9:7788", "token": "abc123",
                   "project": "MyGame"}
