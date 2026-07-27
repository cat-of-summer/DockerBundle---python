"""Staging rules for the build context."""

from __future__ import annotations

from render.writer import stage_file


def test_crlf_is_normalised_when_staging(tmp_path):
    # A Windows checkout hands us CRLF. Copied verbatim into the image, a shebang ending
    # in \r makes the Linux kernel report "no such file or directory".
    source = tmp_path / "entrypoint.sh"
    source.write_bytes(b"#!/bin/sh\r\nset -e\r\nexec \"$@\"\r\n")

    destination = tmp_path / "staged.sh"
    stage_file(source, destination)

    assert b"\r\n" not in destination.read_bytes()
    assert destination.read_bytes() == b'#!/bin/sh\nset -e\nexec "$@"\n'


def test_staging_is_byte_identical_regardless_of_line_endings(tmp_path):
    # The context digest recorded in bundle.lock.yml must not depend on how git checked
    # the sources out, or the same commit produces different lock files per platform.
    crlf = tmp_path / "crlf.conf"
    crlf.write_bytes(b"server {\r\n    listen 80;\r\n}\r\n")
    lf = tmp_path / "lf.conf"
    lf.write_bytes(b"server {\n    listen 80;\n}\n")

    stage_file(crlf, tmp_path / "a")
    stage_file(lf, tmp_path / "b")

    assert (tmp_path / "a").read_bytes() == (tmp_path / "b").read_bytes()


def test_binary_files_are_copied_untouched(tmp_path):
    # 0x0d 0x0a inside binary content is data, not a line ending.
    payload = b"\x89PNG\r\n\x1a\n\x00\x01\x02\r\n\xff"
    source = tmp_path / "image.png"
    source.write_bytes(payload)

    destination = tmp_path / "out.png"
    stage_file(source, destination)

    assert destination.read_bytes() == payload


def test_invalid_utf8_is_copied_untouched(tmp_path):
    payload = b"\xff\xfe\x00binary\r\ndata"
    source = tmp_path / "blob.bin"
    source.write_bytes(payload)

    destination = tmp_path / "out.bin"
    stage_file(source, destination)

    assert destination.read_bytes() == payload
