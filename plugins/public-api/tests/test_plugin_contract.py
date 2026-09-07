from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib
from determinflow_plugin_public_api.backend import extension as extension_module
from determinflow_plugin_public_api.backend.extension import create_extension
from fastapi import FastAPI
from src.extension_api import ExtensionManifest
from src.extension_api.registrar import ExtensionContributions, ExtensionRegistrar

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_catalog_and_static_page_define_one_optional_plugin() -> None:
    manifest = tomllib.loads(
        (PLUGIN_ROOT / "extension.toml").read_text(encoding="utf-8")
    )
    extension = manifest["extension"]
    page = manifest["page"]
    header_status = manifest["header_status"]

    assert extension["id"] == "public-api"
    assert extension["name"] == "笔枢公益模型"
    assert extension["version"] == "0.1.37"
    assert extension["description"] == "由笔枢写作免费提供的模型体验服务。"
    assert extension["backend"].startswith("determinflow_plugin_public_api.")
    assert "settings" not in manifest
    assert page == {
        "label": "公益模型",
        "static_dir": "ui",
        "entrypoint": "index.html",
        "show_in_details": False,
    }
    assert header_status == {
        "endpoint": "/api/public-api/status",
        "refresh_endpoint": "/api/public-api/renew",
    }
    assert (PLUGIN_ROOT / "ui" / "index.html").is_file()
    assert (PLUGIN_ROOT / "ui" / "app.js").is_file()

    catalog = tomllib.loads(
        (PLUGIN_ROOT.parents[1] / "plugin-repository.toml").read_text(encoding="utf-8")
    )
    assert any(item["id"] == "public-api" for item in catalog["plugins"])


def test_plugin_source_does_not_import_core_model_manager_or_desktop_code() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PLUGIN_ROOT / "determinflow_plugin_public_api").rglob("*.py")
    )
    assert "src.core.model_manager" not in sources
    assert "desktop.python" not in sources


def test_extension_is_inert_outside_supported_desktop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.delenv("DETERMINFLOW_DESKTOP", raising=False)
        monkeypatch.delenv("DETERMINFLOW_PUBLIC_API_DEVELOPMENT", raising=False)
        monkeypatch.setattr(extension_module.sys, "platform", "darwin")
        extension = create_extension()
        manifest = ExtensionManifest(
            extension_id="public-api",
            name="笔枢公益模型",
            version="0.1.37",
        )
        contributions = ExtensionContributions()
        extension.register(ExtensionRegistrar(manifest, contributions))
        assert len(contributions.routers) == 1

        account_session = SimpleNamespace(
            installation_id="desktop:test",
            access_token=lambda: None,
        )
        services = {
            "plugin_config": {},
            "plugin_data_dir": tmp_path,
            "account_session": account_session,
        }
        runtime = SimpleNamespace(
            app=FastAPI(),
            resource_owner="public-api",
            get_service=lambda name, default=None: services.get(name, default),
        )
        try:
            await extension.start(runtime)
            assert extension.service is not None
            status = extension.service.status()
            assert status.state == "disabled"
            assert status.last_error == "仅支持 Windows 和 macOS 桌面版"
            assert status.header_status is not None
            assert status.header_status.summary == status.last_error
            assert extension.service.account_session is account_session
        finally:
            await extension.stop()

    asyncio.run(scenario())


def test_non_windows_development_override_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DETERMINFLOW_DESKTOP", raising=False)
    monkeypatch.setenv("DETERMINFLOW_PUBLIC_API_DEVELOPMENT", "1")
    monkeypatch.setattr(extension_module.sys, "platform", "darwin")

    assert extension_module._runtime_access() == (True, "development", None)


def test_ui_leaves_account_login_to_core_without_collecting_credentials() -> None:
    script = (PLUGIN_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    page = (PLUGIN_ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "console.log" not in script
    assert "/api/public-api" in script
    assert 'request("/login", { method: "POST" })' not in script
    assert 'id="account"' not in page
    assert 'type="password"' not in page
    assert 'name="email"' not in page
    assert "model_page_recharge_enabled" in script
    assert "provider_display_name" in script
    assert 'id="service-notice"' in page
    assert 'id="announcement-feed"' in page
    assert "renderAnnouncements(status.announcements)" in script
    assert 'id="catalog-note"' in page
    assert "全部 ${status.model_catalog.length} 个" in script
    assert "title.textContent = item.title" in script
    assert "body.textContent = item.body" in script
    assert 'status.balance_tier === "paid"' in script
    assert "noopener,noreferrer" in script
    assert "original_input_price" in script
    assert "original_cache_hit_price" in script
    assert 'renderPriceCell(model.prices, "cache_hit_price")' in script
    assert "price-original" in script
    assert "value.append(original)" in script
    assert "function renderModelCell(model)" in script
    assert "label.textContent = text" in script
    assert 'cell.className = "price-cell"' in script
    assert 'label.setAttribute("aria-hidden", "true")' not in script
    assert 'value.className = "price-value"' in script
    assert page.count('class="price-heading"') == 3
    assert "button-accent" in page
    assert "匿名 ·" not in page
    assert "合计可用" not in page
    assert "公益可用" not in page
    assert "今日限额余量" not in page
    assert "本周限额余量" not in page
    assert "今日免费额度" in page
    assert "本周免费额度" in page
    assert 'id="wallet-row" hidden' in page
    assert "elements.walletRow.hidden = !status.signed_in" in script
    assert 'id="error"' in page
    assert "accessLabel(status)" in script
    assert "status.quota?.remaining_usd" in script


def test_older_core_keeps_legacy_session_without_migrating_credentials(tmp_path, monkeypatch):
    import json
    from determinflow_plugin_public_api.backend.legacy_service import LegacyCredentialService

    monkeypatch.delenv("DETERMINFLOW_DESKTOP", raising=False)
    monkeypatch.delenv("DETERMINFLOW_PUBLIC_API_DEVELOPMENT", raising=False)
    state = {
        "schema_version": 2,
        "installation_id": "plugin:compatibility-test",
        "portal_session": {"access_token": "test-access", "refresh_token": "test-refresh"},
        "credential": None,
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    services = {"plugin_config": {}, "plugin_data_dir": tmp_path}
    runtime = SimpleNamespace(
        app=FastAPI(), resource_owner="public-api",
        get_service=lambda name, default=None: services.get(name, default),
    )

    async def scenario():
        extension = create_extension()
        try:
            await extension.start(runtime)
            assert isinstance(extension.service, LegacyCredentialService)
            assert extension.service.status().signed_in is True
            saved = json.loads((tmp_path / "state.json").read_text())
            assert saved["schema_version"] == 2
            assert saved["portal_session"] == state["portal_session"]
        finally:
            await extension.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_public_api_supports_release_desktops(monkeypatch, platform):
    monkeypatch.setenv("DETERMINFLOW_DESKTOP", "1")
    monkeypatch.delenv("DETERMINFLOW_PUBLIC_API_DEVELOPMENT", raising=False)
    monkeypatch.setattr(extension_module.sys, "platform", platform)
    assert extension_module._runtime_access() == (True, "stable", None)


def test_linux_desktop_is_not_silently_enabled(monkeypatch):
    monkeypatch.setenv("DETERMINFLOW_DESKTOP", "1")
    monkeypatch.delenv("DETERMINFLOW_PUBLIC_API_DEVELOPMENT", raising=False)
    monkeypatch.setattr(extension_module.sys, "platform", "linux")
    assert extension_module._runtime_access()[0] is False
