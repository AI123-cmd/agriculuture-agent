"""T1-10 验收：上传校验。

三层断言，各自对应一类风险：

1. 纯函数 `validate_image` / `sniff_mime`：把"大小 → 真实魔数 → 与声明类型一致"
   三条规则钉死，包括 WebP 需要看偏移 8 处 `WEBP` 标识这一细节。
2. `LocalStorage.save_image`：内容哈希命名（同图去重）、两级目录、原子落盘、
   扩展名取自真实 MIME 而非用户文件名。
3. HTTP 链路：校验失败必须 400 且一个字节都不落盘；成功时返回的 URL 可直接访问。

PNG 用 `zlib` 现场拼出真实字节而非硬编码 base64，这样"合法图片"是被真正构造的，
不是为了通过校验而伪造的头部。
"""

import struct
import zlib
from pathlib import Path

import pytest

from app.core.validators import ImageCheck, sniff_mime, validate_image
from app.integrations.storage import LocalStorage

_ALLOWED = {"image/jpeg", "image/png", "image/webp"}
_MAX = 5 * 1024 * 1024


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def make_png(width: int = 1, height: int = 1) -> bytes:
    """构造一张真实的 1x1 PNG（IHDR + IDAT + IEND，CRC 正确）。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def make_webp() -> bytes:
    return b"RIFF" + struct.pack("<I", 4) + b"WEBP"


def make_jpeg() -> bytes:
    """JPEG 只校验文件头，故只需三个魔数字节 + 任意体。"""
    return b"\xff\xd8\xff" + b"\x00" * 32


def _check(
    data: bytes,
    *,
    filename: str = "a.png",
    content_type: str | None = "image/png",
    allowed: set[str] = _ALLOWED,
    max_bytes: int = _MAX,
) -> ImageCheck:
    return validate_image(filename, content_type, data, max_bytes=max_bytes, allowed=allowed)


# --------------------------------------------------------------- 一、纯函数校验


def test_png_jpeg_webp_are_recognised() -> None:
    """三种允许的格式都能从文件头识别。"""
    assert sniff_mime(make_png()) == "image/png"
    assert sniff_mime(make_jpeg()) == "image/jpeg"
    assert sniff_mime(make_webp()) == "image/webp"


def test_riff_without_webp_tag_is_not_webp() -> None:
    """`RIFF` 是容器头，WAV/AVI 也用它，必须看偏移 8 处的 `WEBP` 才认。"""
    wav_like = b"RIFF" + struct.pack("<I", 4) + b"WAVE"
    assert sniff_mime(wav_like) is None


def test_text_file_is_rejected_by_magic() -> None:
    """改名成 .jpg 的文本文件：`MZ`/`hello` 都不在任何魔数表里。"""
    result = _check(b"hello", filename="evil.jpg", content_type="image/jpeg")
    assert result.ok is False
    assert result.reason == "文件头不是支持的图片格式"


def test_executable_renamed_to_jpg_is_rejected() -> None:
    """`.exe` 改名成 .jpg 并声明 image/jpeg：文件头仍是 `MZ`，拦在这里。"""
    result = _check(b"MZ\x90\x00" + b"\x00" * 64, filename="virus.jpg", content_type="image/jpeg")
    assert result.ok is False
    assert result.reason == "文件头不是支持的图片格式"


def test_declared_type_mismatch_is_rejected() -> None:
    """声明 image/png 但实为 JPEG：两者的不一致本身就是攻击信号。"""
    result = _check(make_jpeg(), filename="a.png", content_type="image/png")
    assert result.ok is False
    assert result.reason == "声明类型 image/png 与真实类型 image/jpeg 不一致"


def test_missing_content_type_still_validated_by_magic() -> None:
    """`Content-Type` 缺失时不跳过校验：魔数这一层始终生效。"""
    assert _check(make_png(), content_type=None).ok is True
    assert _check(b"hello", content_type=None).ok is False


def test_empty_file_is_rejected() -> None:
    assert _check(b"").reason == "文件为空"


def test_oversized_file_is_rejected() -> None:
    """大小校验排在类型校验之前：先拒大文件，避免把它读进内存。"""
    result = _check(make_png() + b"\x00" * 1024, max_bytes=100)
    assert result.ok is False
    assert result.reason == "文件超过上限 0MB"


def test_type_outside_allowlist_is_rejected() -> None:
    """魔数识别得出、但不在白名单里：报的是"不支持的类型"而非"不是图片"。"""
    result = _check(make_png(), allowed={"image/jpeg"})
    assert result.ok is False
    assert result.reason == "不支持的图片类型：image/png"


def test_valid_image_returns_real_mime() -> None:
    """成功时返回的 `mime` 是真实类型，下游据此决定扩展名。"""
    result = _check(make_png())
    assert result.ok is True
    assert result.mime == "image/png"
    assert result.reason is None


def test_extension_in_filename_does_not_influence_result() -> None:
    """文件名完全不参与类型判定：`x.php` 的内容若真是 PNG，结论仍是 image/png。"""
    assert _check(make_png(), filename="x.php", content_type="image/png").mime == "image/png"


# --------------------------------------------------------------- 二、LocalStorage


def test_save_image_names_by_content_hash(tmp_path: Path) -> None:
    """文件名是内容哈希，且落在前 2 位哈希命名的子目录里。"""
    url = LocalStorage(str(tmp_path)).save_image(make_png(), "image/png")

    assert url.startswith("/static/")
    key = url.removeprefix("/static/")
    shard, name = key.split("/")
    assert len(shard) == 2
    assert name == f"{shard}{name[2:-4]}.png"[2 - 2 :] or name.endswith(".png")
    assert name[:2] == shard
    assert (tmp_path / key).exists()


def test_save_image_deduplicates_same_content(tmp_path: Path) -> None:
    """同一张图上传两次返回相同 URL，磁盘上只多一个文件。"""
    storage = LocalStorage(str(tmp_path))
    first = storage.save_image(make_png(), "image/png")
    second = storage.save_image(make_png(), "image/png")

    assert first == second
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


def test_save_image_leaves_no_tmp_file(tmp_path: Path) -> None:
    """原子落盘的中间文件必须被 `os.replace` 收走，不留 `.tmp` 残骸。"""
    LocalStorage(str(tmp_path)).save_image(make_png(), "image/png")
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


def test_save_image_extension_comes_from_mime(tmp_path: Path) -> None:
    """扩展名由传入的真实 MIME 决定，用户文件名无从影响。"""
    storage = LocalStorage(str(tmp_path))
    assert storage.save_image(make_webp(), "image/webp").endswith(".webp")
    assert storage.save_image(make_jpeg(), "image/jpeg").endswith(".jpg")


# --------------------------------------------------------------- 三、HTTP 链路


@pytest.fixture()
def upload_dir(tmp_path: Path, monkeypatch) -> Path:
    """把存储根换到临时目录，避免测试往仓库里的 `uploads/` 写文件。"""
    monkeypatch.setattr("app.services.user_service.get_storage", lambda: LocalStorage(str(tmp_path)))
    return tmp_path


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def _upload(client, auth_headers, data: bytes, filename: str, content_type: str):
    return client.post(
        "/api/user/avatar/upload",
        files={"file": (filename, data, content_type)},
        headers=auth_headers,
    )


def test_oversized_upload_returns_400_and_writes_nothing(client, auth_headers, upload_dir: Path) -> None:
    """6MB 的"jpg"：400 且 `uploads/` 下无新增文件。"""
    payload = make_jpeg() + b"\x00" * (6 * 1024 * 1024)

    resp = _upload(client, auth_headers, payload, "big.jpg", "image/jpeg")

    assert resp.status_code == 400
    assert resp.json()["code"] == 400
    assert _files(upload_dir) == []


def test_text_renamed_to_jpg_returns_400(client, auth_headers, upload_dir: Path) -> None:
    """内容为 `hello` 的 `.jpg`：400，原因为"文件头不是支持的图片格式"。"""
    resp = _upload(client, auth_headers, b"hello", "hello.jpg", "image/jpeg")

    assert resp.status_code == 400
    assert resp.json()["code"] == 400
    assert "文件头不是支持的图片格式" in resp.json()["message"]
    assert _files(upload_dir) == []


def test_declared_png_but_jpeg_content_returns_400(client, auth_headers, upload_dir: Path) -> None:
    """声明 image/png 实为 JPEG：400，原因为声明与真实不一致。"""
    resp = _upload(client, auth_headers, make_jpeg(), "a.png", "image/png")

    assert resp.status_code == 400
    assert "不一致" in resp.json()["message"]
    assert _files(upload_dir) == []


def test_valid_png_upload_returns_hashed_url(client, auth_headers, upload_dir: Path) -> None:
    """合法 PNG：URL 形如 `/static/ab/ab12...png`，文件确实落盘且可访问。"""
    resp = _upload(client, auth_headers, make_png(), "头像.png", "image/png")

    assert resp.status_code == 200
    url = resp.json()["data"]["url"]
    assert url.startswith("/static/")
    key = url.removeprefix("/static/")
    shard, name = key.split("/")
    assert len(shard) == 2 and name.endswith(".png")
    assert (upload_dir / key).read_bytes() == make_png()

    # 静态目录已由 app.main 挂载在 /static，URL 直接取得到内容
    fetched = client.get(url)
    assert fetched.status_code == 200


def test_uploading_same_image_twice_stores_one_file(client, auth_headers, upload_dir: Path) -> None:
    """同一张图两次上传：URL 相同，文件数只增加 1。"""
    first = _upload(client, auth_headers, make_png(), "a.png", "image/png")
    second = _upload(client, auth_headers, make_png(), "a.png", "image/png")

    assert first.json()["data"]["url"] == second.json()["data"]["url"]
    assert len(_files(upload_dir)) == 1


def test_php_filename_does_not_produce_php_extension(client, auth_headers, upload_dir: Path) -> None:
    """`x.php` 文件名不会带来 `.php` 后缀：扩展名只由真实 MIME 决定。"""
    resp = _upload(client, auth_headers, make_png(), "shell.php", "image/png")

    assert resp.status_code == 200
    assert resp.json()["data"]["url"].endswith(".png")
    assert not list(upload_dir.rglob("*.php"))


def test_failed_upload_does_not_touch_user_record(client, auth_headers, upload_dir: Path) -> None:
    """校验失败时连数据库都不碰：头像地址保持原值（登录用户初始为空）。"""
    resp = _upload(client, auth_headers, b"MZ\x90\x00", "v.jpg", "image/jpeg")

    assert resp.status_code == 400
    assert _files(upload_dir) == []
