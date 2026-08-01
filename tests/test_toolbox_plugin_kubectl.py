"""No real kubectl needed: build_tools() is pure Python given a fake
DetectionResult, and build_args functions are pure functions over dicts."""

from pcli.tools.toolbox.plugin_base import DetectionResult
from pcli.tools.toolbox.plugins.httpd import HttpdPlugin
from pcli.tools.toolbox.plugins.kafka import KafkaPlugin
from pcli.tools.toolbox.plugins.kubectl import KubectlPlugin
from pcli.tools.toolbox.plugins.sge import SgePlugin


def _fake_detection(name: str, binary_path: str, version_string: str, version=(1, 0, 0)) -> DetectionResult:
    return DetectionResult(
        software_name=name, binary_path=binary_path, version_string=version_string, version=version
    )


def test_kubectl_parse_version():
    plugin = KubectlPlugin()
    assert plugin.parse_version("Client Version: v1.29.3\n") == (1, 29, 3)
    assert plugin.parse_version("garbage") is None


def test_kubectl_build_tools_shape_and_risk_tiers():
    plugin = KubectlPlugin()
    detection = _fake_detection("kubectl", "/usr/bin/kubectl", "v1.29.3", (1, 29, 3))
    tools = plugin.build_tools(detection)
    names = {t.name for t in tools}
    assert {"get", "describe", "logs", "get_contexts", "apply", "scale", "delete"} <= names

    by_name = {t.name: t for t in tools}
    assert by_name["get"].risk == "read"
    assert by_name["apply"].risk == "mutate"
    assert by_name["delete"].risk == "destructive"
    assert all(t.binary_path == "/usr/bin/kubectl" for t in tools)


def test_kubectl_get_build_args():
    plugin = KubectlPlugin()
    detection = _fake_detection("kubectl", "/usr/bin/kubectl", "v1.29.3")
    get_spec = next(t for t in plugin.build_tools(detection) if t.name == "get")

    assert get_spec.build_args({"resource": "pods"}) == ["get", "pods"]
    assert get_spec.build_args({"resource": "pods", "namespace": "kube-system"}) == [
        "get",
        "pods",
        "--namespace",
        "kube-system",
    ]
    assert get_spec.build_args({"resource": "pods", "all_namespaces": True, "namespace": "ignored"}) == [
        "get",
        "pods",
        "--all-namespaces",
    ]
    assert get_spec.build_args({"resource": "pods", "name": "web-1", "output": "json"}) == [
        "get",
        "pods",
        "web-1",
        "-o",
        "json",
    ]


def test_kubectl_delete_build_args():
    plugin = KubectlPlugin()
    detection = _fake_detection("kubectl", "/usr/bin/kubectl", "v1.29.3")
    delete_spec = next(t for t in plugin.build_tools(detection) if t.name == "delete")
    assert delete_spec.build_args({"resource": "pod", "name": "web-1", "namespace": "default"}) == [
        "delete",
        "pod",
        "web-1",
        "--namespace",
        "default",
    ]


def test_httpd_parse_version_and_tools():
    plugin = HttpdPlugin()
    assert plugin.parse_version("Server version: Apache/2.4.58 (Unix)") == (2, 4, 58)
    detection = _fake_detection("httpd", "/usr/sbin/apachectl", "Apache/2.4.58")
    tools = plugin.build_tools(detection)
    by_name = {t.name: t for t in tools}
    assert by_name["configtest"].build_args({}) == ["-t"]
    assert by_name["stop"].risk == "destructive"
    assert by_name["configtest"].risk == "read"


def test_sge_build_tools_skips_missing_siblings(monkeypatch):
    import pcli.tools.toolbox.plugins.sge as sge_module

    def _fake_which(name: str):
        return f"/usr/bin/{name}" if name in ("qstat", "qsub") else None

    monkeypatch.setattr(sge_module.shutil, "which", _fake_which)

    plugin = SgePlugin()
    detection = _fake_detection("sge", "/usr/bin/qstat", "unknown", (0,))
    tools = plugin.build_tools(detection)
    names = {t.name for t in tools}
    assert names == {"qstat", "qsub"}  # qhost/qacct/qdel weren't "found" -> excluded


def test_sge_qstat_build_args():
    plugin = SgePlugin()
    detection = _fake_detection("sge", "/usr/bin/qstat", "unknown", (0,))
    qstat_spec = next(t for t in plugin.build_tools(detection) if t.name == "qstat")
    assert qstat_spec.build_args({}) == []
    assert qstat_spec.build_args({"all_users": True}) == ["-u", "*"]
    assert qstat_spec.build_args({"job_id": 123, "full": True}) == ["-j", "123", "-f"]


def test_kafka_build_tools_without_consumer_groups(monkeypatch):
    import pcli.tools.toolbox.plugins.kafka as kafka_module

    monkeypatch.setattr(kafka_module.shutil, "which", lambda name: None)

    plugin = KafkaPlugin()
    detection = _fake_detection("kafka", "/opt/kafka/bin/kafka-topics.sh", "unknown")
    tools = plugin.build_tools(detection)
    names = {t.name for t in tools}
    assert names == {"list_topics", "describe_topic", "create_topic", "delete_topic"}


def test_kafka_create_topic_build_args():
    plugin = KafkaPlugin()
    detection = _fake_detection("kafka", "/opt/kafka/bin/kafka-topics.sh", "unknown")
    create_spec = next(t for t in plugin.build_tools(detection) if t.name == "create_topic")
    argv = create_spec.build_args(
        {"bootstrap_server": "localhost:9092", "topic": "events", "partitions": 3}
    )
    assert argv == [
        "--bootstrap-server",
        "localhost:9092",
        "--create",
        "--topic",
        "events",
        "--partitions",
        "3",
        "--replication-factor",
        "1",
    ]
