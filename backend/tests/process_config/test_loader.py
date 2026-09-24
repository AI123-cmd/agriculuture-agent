"""T2-03：加载器与跨字段一致性校验。

四类用例对应四种失败方式：文件缺失、YAML 根节点非法、字段校验失败、跨字段矛盾。
其中跨字段矛盾（红线为空）最容易被误当成"模型已经校验过了"，因此单列一条。
"""

import logging

import pytest
import yaml

from app.agent.state import ProcessId
from app.process_config import loader
from app.process_config.errors import ConfigIntegrityError, ConfigLoadError


def _valid_payload(**overrides) -> dict:
    base = {
        "process_id": "rice",
        "version": 1,
        "display_name": "水稻",
        "role_definition": "水稻种植顾问",
        "required_slots": [
            {
                "key": "rice_symptom_part",
                "label": "受害部位",
                "display_label": "水稻哪个部位出问题了",
                "group": "病虫情",
                "owner": "interviewer",
            }
        ],
        "redlines": [
            {
                "id": "RICE-01",
                "desc": "不得推荐未在水稻上登记的农药",
                "rule": "R-REGISTERED-CROP",
                "severity": "block",
            }
        ],
        "terminology": {
            "internal_terms": {"病丛率": "发病丛数占总丛数的比例"},
            "colloquial_map": {"病丛率": "有多少丛稻子出问题"},
            "display_metrics": ["病丛率"],
        },
        "timing_rules": [],
        "output_template": {"section_order": ["现状判断"], "required_sections": ["现状判断"]},
        "calendar_source": "rice.growth_stage",
        "followup_policy": {"triggers": [{"after_days": 7, "ask": "效果如何"}]},
        "offline_questions": [],
        "confusable_items": [],
    }
    base.update(overrides)
    return base


@pytest.fixture()
def temp_seeds(tmp_path, monkeypatch):
    """把加载器的 seeds 目录指向临时目录。

    这样四类用例都不依赖 T2-04 的真实 YAML：一是本项要能在 T2-04 之前独立验收，
    二是"坏配置"的构造不必污染真实 seeds 目录。
    """

    def _fake_files(package: str):
        assert package == loader.SEEDS_PACKAGE
        return tmp_path

    monkeypatch.setattr(loader, "files", _fake_files)
    return tmp_path


def _write(dir_path, process_id: str, payload) -> None:
    text = payload if isinstance(payload, str) else yaml.safe_dump(payload, allow_unicode=True)
    (dir_path / f"{process_id}.yaml").write_text(text, encoding="utf-8")


def test_load_valid_config(temp_seeds) -> None:
    _write(temp_seeds, "rice", _valid_payload())
    config = loader.load_process_config(ProcessId.RICE)
    assert config.process_id is ProcessId.RICE
    assert config.display_name == "水稻"


def test_missing_file_raises(temp_seeds) -> None:
    with pytest.raises(ConfigLoadError) as exc:
        loader.load_process_config(ProcessId.TEA)
    assert "tea" in str(exc.value)


def test_yaml_syntax_error_raises(temp_seeds) -> None:
    _write(temp_seeds, "chili", "process_id: [unclosed")
    with pytest.raises(ConfigLoadError) as exc:
        loader.load_process_config(ProcessId.CHILI)
    assert "YAML 语法错误" in str(exc.value)


def test_non_mapping_root_raises(temp_seeds) -> None:
    _write(temp_seeds, "weed", "- 1\n- 2\n")
    with pytest.raises(ConfigLoadError) as exc:
        loader.load_process_config(ProcessId.WEED)
    assert "根节点" in str(exc.value)


def test_unknown_field_raises(temp_seeds) -> None:
    _write(temp_seeds, "rice", _valid_payload(nonexistent_key="x"))
    with pytest.raises(ConfigLoadError) as exc:
        loader.load_process_config(ProcessId.RICE)
    assert "配置校验失败" in str(exc.value)


def test_empty_redlines_raises_integrity(temp_seeds) -> None:
    """红线为空的流程必须处于不可用状态（技术方案 8.4）。"""
    _write(temp_seeds, "rice", _valid_payload(redlines=[]))
    with pytest.raises(ConfigIntegrityError) as exc:
        loader.load_process_config(ProcessId.RICE)
    assert "redlines 为空" in str(exc.value)
    assert exc.value.process_id == "rice"


def test_duplicate_slot_key_raises_integrity(temp_seeds) -> None:
    slot = {
        "key": "dup",
        "label": "A",
        "display_label": "a",
        "group": "病虫情",
        "owner": "interviewer",
    }
    _write(temp_seeds, "rice", _valid_payload(required_slots=[slot, dict(slot)]))
    with pytest.raises(ConfigIntegrityError) as exc:
        loader.load_process_config(ProcessId.RICE)
    assert "重复" in str(exc.value)


def test_unknown_owner_raises_integrity(temp_seeds) -> None:
    slot = {
        "key": "a",
        "label": "A",
        "display_label": "a",
        "group": "病虫情",
        "owner": "not_implemented_agent",
    }
    _write(temp_seeds, "rice", _valid_payload(required_slots=[slot]))
    with pytest.raises(ConfigIntegrityError) as exc:
        loader.load_process_config(ProcessId.RICE)
    assert "owner 未实现" in str(exc.value)


def test_panel_without_confusable_items_raises_integrity(temp_seeds) -> None:
    _write(temp_seeds, "rice", _valid_payload(panel_enabled=True, confusable_items=[]))
    with pytest.raises(ConfigIntegrityError) as exc:
        loader.load_process_config(ProcessId.RICE)
    assert "易混淆项" in str(exc.value)


def test_required_section_outside_order_raises_integrity(temp_seeds) -> None:
    """必含小节不在段落顺序内会在渲染时静默丢段落，属于「配置写了但没生效」。"""
    _write(
        temp_seeds,
        "rice",
        _valid_payload(output_template={"section_order": ["现状判断"], "required_sections": ["用药安全提示"]}),
    )
    with pytest.raises(ConfigIntegrityError) as exc:
        loader.load_process_config(ProcessId.RICE)
    assert "section_order" in str(exc.value)


def test_duplicate_timing_rule_id_raises_integrity(temp_seeds) -> None:
    rule = {"id": "dup_rule", "when": {"season": "spring"}, "prompt": "p"}
    _write(temp_seeds, "rice", _valid_payload(timing_rules=[rule, dict(rule)]))
    with pytest.raises(ConfigIntegrityError) as exc:
        loader.load_process_config(ProcessId.RICE)
    assert "timing_rules" in str(exc.value)


def test_rapeseed_requires_pesticide_ban_redline(temp_seeds) -> None:
    """产品说明书 5.4：油菜流程必须有禁止输出药剂推荐的红线。"""
    _write(temp_seeds, "rapeseed", _valid_payload(process_id="rapeseed"))
    with pytest.raises(ConfigIntegrityError) as exc:
        loader.load_process_config(ProcessId.RAPESEED)
    assert "RAPE-01" in str(exc.value)


def test_rapeseed_with_ban_redline_passes(temp_seeds) -> None:
    _write(
        temp_seeds,
        "rapeseed",
        _valid_payload(
            process_id="rapeseed",
            redlines=[{"id": "RAPE-01", "desc": "禁止输出药剂推荐类内容", "rule": "R-REDLINE", "severity": "block"}],
        ),
    )
    config = loader.load_process_config(ProcessId.RAPESEED)
    assert config.process_id is ProcessId.RAPESEED


def test_load_all_skips_broken_and_logs(temp_seeds, caplog) -> None:
    """单条流程坏掉不应让整体加载失败，但必须留下 ERROR 日志。"""
    _write(temp_seeds, "rice", _valid_payload())
    _write(temp_seeds, "tea", "process_id: [unclosed")

    with caplog.at_level(logging.ERROR, logger=loader.__name__):
        configs = loader.load_all_configs()

    assert set(configs) == {ProcessId.RICE}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == len(ProcessId) - 1
    assert any(getattr(r, "process_id", None) == "tea" for r in errors)


def test_cache_returns_same_instance_and_refresh_reloads(temp_seeds) -> None:
    _write(temp_seeds, "rice", _valid_payload())
    loader.clear_config_cache()
    first = loader.get_process_config(ProcessId.RICE)
    assert loader.get_process_config(ProcessId.RICE) is first

    _write(temp_seeds, "rice", _valid_payload(version=2))
    assert loader.get_process_config(ProcessId.RICE) is first  # 未 refresh 时命中缓存
    second = loader.get_process_config(ProcessId.RICE, refresh=True)
    assert second.version == 2
    loader.clear_config_cache()
