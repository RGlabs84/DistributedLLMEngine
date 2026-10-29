"""
engine/media.py: what the coding endpoint accepts as an image before the engine sees one.

The files are built in tests/media_samples.py: a PNG is a real, small one (the validator inflates its data to bound
it), a JPEG is a header and a frame header (the validator reads only those).
"""

import base64
import struct
import zlib

import pytest

from engine import media
from tests.media_samples import jpeg, png, png_chunk, png_with_stream, raw_rows


def data_url(raw, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def image_part(url):
    return {"type": "image_url", "image_url": {"url": url}}


def user(*parts):
    return [{"role": "user", "content": list(parts)}]


def refused(messages):
    with pytest.raises(media.ImageError) as excinfo:
        media.validate_images(messages)
    return excinfo.value


# --- what is accepted ---------------------------------------------------------------------

@pytest.mark.parametrize("raw, kind", [
    (png(640, 480), "png"),
    (jpeg(640, 480), "jpeg"),
    (jpeg(640, 480, marker=0xC1), "jpeg"),                       # extended sequential Huffman: stb_image reads it
    (jpeg(640, 480, marker=0xC2), "jpeg"),                       # progressive
    (jpeg(640, 480, before=b"\xff\xff"), "jpeg"),                # a fill byte before the frame header
    (jpeg(640, 480, components=1, sampling=(0x11,)), "jpeg"),    # gray
    (jpeg(640, 480, components=4, sampling=(0x11,)), "jpeg"),    # CMYK
])
def test_png_and_jpeg_are_read_from_their_headers(raw, kind):
    (info,) = media.validate_images(user(image_part(data_url(raw, "image/" + kind))))

    assert (info.format, info.width, info.height) == (kind, 640, 480)


@pytest.mark.parametrize("color, depth, channels", [
    (0, 1, 1), (0, 2, 1), (0, 4, 1), (0, 8, 1), (0, 16, 1), (2, 8, 3), (2, 16, 3), (4, 8, 2), (4, 16, 2),
    (6, 8, 4), (6, 16, 4),
])
@pytest.mark.parametrize("interlace", [0, 1])
def test_every_png_variant_stb_image_decodes_is_accepted(color, depth, channels, interlace):
    width, height = 13, 11
    rows = _adam7_rows(width, height, channels, depth) if interlace else raw_rows(width, height, channels, depth)
    raw = png_with_stream(width, height, zlib.compress(rows), color=color, depth=depth, interlace=interlace)

    (info,) = media.validate_images(user(image_part(data_url(raw))))

    assert (info.format, info.width, info.height) == ("png", width, height)


def test_a_palette_png_with_its_palette_is_accepted_and_without_it_is_not():
    palette = png_chunk(b"PLTE", bytes(range(12)))
    stream = zlib.compress(raw_rows(5, 5, 1, 2))

    assert media.validate_images(user(image_part(data_url(png_with_stream(5, 5, stream, color=3, depth=2, before=palette)))))
    assert refused(user(image_part(data_url(png_with_stream(5, 5, stream, color=3, depth=2))))).code == media.UNSUPPORTED_IMAGE_TYPE
    assert refused(user(image_part(data_url(png_with_stream(5, 5, stream, color=3, depth=2, before=png_chunk(b"PLTE", b"\0" * 4)))))
                   ).code == media.UNSUPPORTED_IMAGE_TYPE


def test_the_data_may_be_split_over_many_idat_chunks_and_ancillary_chunks_are_stepped_over():
    stream = zlib.compress(raw_rows(300, 300), 0)           # stored: the stream is long enough to split
    side = png_chunk(b"tEXt", b"parameters\0a prompt") + png_chunk(b"pHYs", bytes(9)) + png_chunk(b"acTL", bytes(8))

    (info,) = media.validate_images(user(image_part(data_url(png_with_stream(300, 300, stream, before=side, idat_split=500)))))

    assert (info.width, info.height) == (300, 300)


def _adam7_rows(width, height, channels, depth):
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    rows = b""
    for x0, y0, dx, dy in passes:
        across, down = (width - x0 + dx - 1) // dx, (height - y0 + dy - 1) // dy
        if across > 0 and down > 0:
            rows += raw_rows(across, down, channels, depth)
    return rows


# --- WebP and everything else stb_image does not read ---------------------------------------------------------
# llama.cpp hands a file stb_image cannot read to ffmpeg; the engine is started with that path pointed at an empty
# directory, and the validator accepts only what stb_image decodes, so none of these reach the engine.

def _webp(width=8, height=8):
    return b"RIFF" + bytes(4) + b"WEBPVP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", (width - 1) | ((height - 1) << 14))


@pytest.mark.parametrize("raw", [
    _webp(), b"RIFF" + bytes(4) + b"WEBPVP8 " + bytes(4) + bytes(3) + b"\x9d\x01\x2a" + struct.pack("<HH", 8, 8),
    b"RIFF" + bytes(4) + b"WEBPVP8X" + struct.pack("<I", 10) + bytes(4) + bytes(6),
])
@pytest.mark.parametrize("mime", ["image/webp", "image/png"])
def test_webp_is_refused_whatever_the_mime_says(raw, mime):
    error = refused(user(image_part(data_url(raw, mime))))

    assert error.code == media.UNSUPPORTED_IMAGE_TYPE and "PNG or JPEG" in error.message


@pytest.mark.parametrize("marker", [0xC3, 0xC5, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCF])
def test_a_jpeg_that_is_lossless_hierarchical_or_arithmetic_coded_is_refused(marker):
    assert refused(user(image_part(data_url(jpeg(8, 8, marker=marker), "image/jpeg")))).code == media.UNSUPPORTED_IMAGE_TYPE


@pytest.mark.parametrize("raw", [
    jpeg(8, 8, precision=12),                                       # 12-bit
    jpeg(8, 8, components=2, sampling=(0x11,)),
    jpeg(8, 8, components=5, sampling=(0x11,)),
    jpeg(8, 8, sampling=(0x21, 0x31, 0x11)),                        # 2 is not a whole multiple of 3: stb_image cannot resample
    jpeg(8, 8, sampling=(0x50, 0x11, 0x11)),                        # a sampling factor of 5, and one of 0
    jpeg(8, 8, before=b"\xff\x01"),                                 # TEM, a marker stb_image does not know
    jpeg(8, 8, before=b"\xff\xcc\x00\x04\x00\x00"),              # arithmetic conditioning (DAC)
    jpeg(8, 8, before=b"\xff\xdd\x00\x05\x00\x00\x00"),         # a restart interval of the wrong length
    jpeg(0, 8), jpeg(8, 0),
])
def test_a_jpeg_stb_image_would_refuse_is_refused(raw):
    assert refused(user(image_part(data_url(raw, "image/jpeg")))).code == media.UNSUPPORTED_IMAGE_TYPE


@pytest.mark.parametrize("mutate", [
    lambda d: d.replace(b"IHDR", b"IHDX"),                                          # not an IHDR first
    lambda d: d[:-12],                                                              # no IEND
    lambda d: d[:40],                                                               # cut inside the data
    lambda d: d[:8] + d[8 + 25:],                                                   # no IHDR at all
    lambda d: _with_ihdr(d, depth=3), lambda d: _with_ihdr(d, color=1), lambda d: _with_ihdr(d, color=5),
    lambda d: _with_ihdr(d, color=2, depth=1),                                      # RGB is 8 or 16 bits
    lambda d: _with_ihdr(d, color=3, depth=16),
    lambda d: _with_ihdr(d, compression=1), lambda d: _with_ihdr(d, filtering=1), lambda d: _with_ihdr(d, interlace=2),
    lambda d: d.replace(b"IDAT", b"IHDR", 1),                                       # a second IHDR
])
def test_a_png_stb_image_would_refuse_is_refused(mutate):
    raw = mutate(png(8, 8))

    assert refused(user(image_part(data_url(raw)))).code in (media.UNSUPPORTED_IMAGE_TYPE, media.IMAGE_TOO_LARGE)


def _with_ihdr(raw, width=None, height=None, depth=None, color=None, compression=None, filtering=None, interlace=None):
    fields = list(struct.unpack(">IIBBBBB", raw[16:29]))
    for index, value in enumerate((width, height, depth, color, compression, filtering, interlace)):
        if value is not None:
            fields[index] = value
    body = struct.pack(">IIBBBBB", *fields)
    return raw[:16] + body + struct.pack(">I", zlib.crc32(b"IHDR" + body) & 0xFFFFFFFF) + raw[33:]


def test_an_unknown_critical_chunk_is_refused_and_an_unknown_ancillary_one_is_not():
    stream = zlib.compress(raw_rows(8, 8))

    assert refused(user(image_part(data_url(png_with_stream(8, 8, stream, before=png_chunk(b"CgBI", bytes(4))))))
                   ).code == media.UNSUPPORTED_IMAGE_TYPE                 # Apple's PNG variant: raw deflate, ffmpeg's job
    assert refused(user(image_part(data_url(png_with_stream(8, 8, stream, before=png_chunk(b"ABCD", b"")))))
                   ).code == media.UNSUPPORTED_IMAGE_TYPE
    assert media.validate_images(user(image_part(data_url(png_with_stream(8, 8, stream, before=png_chunk(b"abcd", b""))))))


def test_a_transparency_chunk_must_fit_the_colour_type():
    stream = zlib.compress(raw_rows(8, 8))

    assert media.validate_images(user(image_part(data_url(png_with_stream(8, 8, stream, before=png_chunk(b"tRNS", bytes(2)))))))
    for bad in (bytes(3), b""):
        assert refused(user(image_part(data_url(png_with_stream(8, 8, stream, before=png_chunk(b"tRNS", bad)))))
                       ).code == media.UNSUPPORTED_IMAGE_TYPE


# --- the PNG decompression bomb ---------------------------------------------------------------------------------

def _zeros_stream(total_bytes, level=9):
    compressor, out, block = zlib.compressobj(level), bytearray(), bytes(1 << 20)
    for _ in range(total_bytes >> 20):
        out += compressor.compress(block)
    return bytes(out + compressor.compress(bytes(total_bytes & ((1 << 20) - 1))) + compressor.flush())


def test_a_one_pixel_png_whose_data_inflates_to_hundreds_of_megabytes_is_refused_and_never_held():
    # stb_image would grow a buffer to hold all of this: 1 MiB of zeros deflates to about 1 KiB, a few MB to GBs.
    bomb = png_with_stream(1, 1, _zeros_stream(512 << 20))

    assert len(bomb) < media.MAX_IMAGE_BYTES                                       # passes the byte limit by a long way
    assert refused(user(image_part(data_url(bomb)))).code == media.IMAGE_TOO_LARGE


def test_the_bomb_is_stopped_after_one_step_not_inflated_whole(monkeypatch):
    sizes = []
    real = zlib.decompressobj

    class Watching:
        def __init__(self):
            self._inner = real()

        def decompress(self, data, max_length=0):
            out = self._inner.decompress(data, max_length)
            sizes.append(len(out))
            return out

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(media.zlib, "decompressobj", Watching)

    refused(user(image_part(data_url(png_with_stream(1, 1, _zeros_stream(64 << 20))))))

    assert 0 < len(sizes) <= 3 and max(sizes) <= media._INFLATE_STEP


def test_a_png_may_not_inflate_past_the_rows_its_header_describes_beyond_a_small_margin():
    exact = raw_rows(32, 32)
    for extra, accepted in ((0, True), (media.PNG_INFLATE_MARGIN, True), (media.PNG_INFLATE_MARGIN + 1, False)):
        raw = png_with_stream(32, 32, zlib.compress(exact + bytes(extra)))
        if accepted:
            assert media.validate_images(user(image_part(data_url(raw))))
        else:
            assert refused(user(image_part(data_url(raw)))).code == media.IMAGE_TOO_LARGE


def test_a_png_that_inflates_to_less_than_its_rows_or_stops_short_is_refused():
    short = png_with_stream(32, 32, zlib.compress(raw_rows(32, 31)))
    cut = png_with_stream(32, 32, zlib.compress(raw_rows(32, 32), 0)[:-40])
    garbage = png_with_stream(32, 32, b"\x78\x9c" + bytes(range(200)))
    nothing = png_with_stream(32, 32, b"")

    for raw in (short, cut, garbage, nothing):
        assert refused(user(image_part(data_url(raw)))).code == media.UNSUPPORTED_IMAGE_TYPE


def test_a_bomb_is_judged_by_the_header_of_its_own_size_not_a_bigger_one():
    # the same data is fine for a 4096 square (2 MiB of one-bit rows) and a bomb for 8 x 8
    stream = zlib.compress(raw_rows(4096, 4096))

    assert media.validate_images(user(image_part(data_url(png_with_stream(4096, 4096, stream)))))
    assert refused(user(image_part(data_url(png_with_stream(8, 8, stream))))).code == media.IMAGE_TOO_LARGE


def test_a_png_may_not_have_more_chunks_or_side_data_than_the_caps():
    stream = zlib.compress(raw_rows(8, 8))
    many = png_chunk(b"tEXt", b"a") * (media.MAX_PNG_CHUNKS + 1)
    heavy = png_chunk(b"iCCP", bytes(media.MAX_PNG_SIDE_BYTES + 1))

    assert refused(user(image_part(data_url(png_with_stream(8, 8, stream, before=many))))).code == media.UNSUPPORTED_IMAGE_TYPE
    assert refused(user(image_part(data_url(png_with_stream(8, 8, stream, before=heavy))))).code == media.UNSUPPORTED_IMAGE_TYPE
    many_idat = png_with_stream(8, 8, zlib.compress(raw_rows(8, 8), 0) + bytes(media.MAX_PNG_IDAT_CHUNKS), idat_split=1)
    assert refused(user(image_part(data_url(many_idat)))).code == media.UNSUPPORTED_IMAGE_TYPE


# --- content part types: only text and inline images, whatever the role ---------------------------------------

@pytest.mark.parametrize("part", [
    {"type": "video_url", "video_url": {"url": "http://192.168.1.1/x"}},
    {"type": "input_video", "input_video": {"url": "http://192.168.1.1/x"}},
    {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
    {"type": "input_image", "image_url": {"url": "http://192.168.1.1/x"}},
    {"type": "IMAGE_URL", "image_url": {"url": "http://192.168.1.1/x"}},
    {"type": None}, {"text": "no type"}, {}, "just a string", 7, None, ["nested"],
])
@pytest.mark.parametrize("role", ["user", "system", "assistant", "tool"])
def test_a_part_type_that_is_not_text_or_an_image_is_refused_on_every_role(role, part):
    with pytest.raises(media.ImageError) as excinfo:
        media.check_content_types([{"role": "user", "content": "hi"}, {"role": role, "content": [part]}])

    assert excinfo.value.code == media.UNSUPPORTED_CONTENT_TYPE


def test_the_unknown_part_is_refused_even_among_good_ones():
    parts = [{"type": "text", "text": "a"}, image_part(data_url(png(2, 2))), {"type": "video_url", "video_url": {"url": "x"}}]

    with pytest.raises(media.ImageError):
        media.check_content_types(user(*parts))


@pytest.mark.parametrize("content", ["text", "", None, [], [{"type": "text", "text": "a"}], [image_part("x")]])
def test_text_and_text_or_image_parts_pass_the_type_check(content):
    media.check_content_types([{"role": "user", "content": content}])


def test_content_that_is_neither_text_nor_a_list_is_refused():
    for content in ({"type": "text", "text": "a"}, 5, True):
        with pytest.raises(media.ImageError) as excinfo:
            media.check_content_types([{"role": "user", "content": content}])
        assert excinfo.value.code == media.UNSUPPORTED_CONTENT_TYPE


def test_messages_that_are_not_messages_are_left_to_the_engine():
    media.check_content_types(None)
    media.check_content_types(["not a dict", 3])
    media.check_content_types([{"role": "user"}])


def test_the_type_is_the_files_own_not_the_one_the_client_declared():
    (info,) = media.validate_images(user(image_part(data_url(png(8, 8), mime="image/jpeg"))))

    assert info.format == "png"


def test_the_whole_4096_square_is_allowed_and_one_pixel_more_is_not():
    assert media.validate_images(user(image_part(data_url(png(4096, 4096)))))[0].width == 4096
    assert refused(user(image_part(data_url(png(4097, 1))))).code == media.IMAGE_TOO_LARGE
    assert refused(user(image_part(data_url(png(1, 4097))))).code == media.IMAGE_TOO_LARGE


def test_images_in_every_message_are_found_in_order_and_text_is_ignored():
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, image_part(data_url(png(1, 2)))]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [image_part(data_url(jpeg(3, 4)))]},
    ]

    assert [(i.width, i.height) for i in media.validate_images(messages)] == [(1, 2), (3, 4)]


def test_no_images_is_not_an_error():
    assert media.validate_images([{"role": "user", "content": "hello"}]) == []
    assert media.validate_images(None) == []


# --- what is refused, and the code it carries -----------------------------------------------

@pytest.mark.parametrize("url", [
    "https://example.com/a.png", "http://127.0.0.1:8080/a.png", "file:///etc/passwd", "ftp://host/a.png",
    "//host/a.png", "/etc/passwd", "gopher://host/", "  HTTP://x/a.png", "javascript:alert(1)",
])
def test_nothing_but_inline_data_is_ever_fetched(url):
    assert refused(user(image_part(url))).code == media.REMOTE_IMAGE_URL


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg"])
def test_exactly_the_two_data_headers_llama_server_reads_are_accepted(mime):
    raw = png(2, 2) if mime == "image/png" else jpeg(2, 2)

    assert media.validate_images(user(image_part(data_url(raw, mime))))


@pytest.mark.parametrize("make", [
    lambda url: "  " + url, lambda url: url + " ", lambda url: url + "\n", lambda url: "\t" + url,
    lambda url: url.replace("data:", "DATA:", 1),                              # llama-server matches "data:" in lowercase only
    lambda url: url.replace("data:", "Data:", 1),
    lambda url: url.replace("image/png", "IMAGE/PNG"),
    lambda url: url.replace("image/png", "image/webp"),
    lambda url: url.replace("image/png", "image/gif"),
    lambda url: url.replace("image/png", "image/jpg"),
    lambda url: url.replace("image/png", "application/octet-stream"),
    lambda url: url.replace("image/png", ""),                                  # data:;base64,...
    lambda url: url.replace(";base64", ";charset=utf-8;base64"),
    lambda url: url.replace(";base64", ";BASE64"),
    lambda url: url.replace(",", ",\n", 1),                                    # whitespace inside the data
])
def test_a_data_url_that_llama_server_would_read_differently_is_refused_here(make):
    error = refused(user(image_part(make(data_url(png(2, 2))))))

    assert error.code == media.UNSUPPORTED_IMAGE_TYPE


@pytest.mark.parametrize("url", [
    "data:image/png,rawbytes",                              # not base64
    "data:image/png;base64,!!!not base64!!!",
    "data:image/png;base64,",                               # nothing
    "",
    None,
])
def test_a_data_url_that_holds_no_image_data_is_refused(url):
    assert refused(user(image_part(url))).code == media.UNSUPPORTED_IMAGE_TYPE


def test_an_image_part_without_an_object_is_refused():
    assert refused(user({"type": "image_url"})).code == media.UNSUPPORTED_IMAGE_TYPE
    assert refused(user({"type": "image_url", "image_url": "data:image/png;base64,AAAA"})).code == media.UNSUPPORTED_IMAGE_TYPE


@pytest.mark.parametrize("raw", [
    b"GIF89a" + bytes(20), b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"%PDF-1.7", b"plain text", b"BM" + bytes(30),
])
def test_other_formats_are_refused_whatever_the_mime_says(raw):
    assert refused(user(image_part(data_url(raw, mime="image/png")))).code == media.UNSUPPORTED_IMAGE_TYPE


@pytest.mark.parametrize("raw", [
    png(1, 1)[:20],                                          # cut inside the header
    b"\x89PNG\r\n\x1a\n" + bytes(30),                        # no IHDR
    b"\xff\xd8\xff",                                         # a JPEG that stops
    b"\xff\xd8\xff\xe0\x00\x10" + bytes(14) + b"\xff\xda\x00\x08" + bytes(6),    # a scan before any frame header
])
def test_a_header_that_does_not_say_its_size_is_refused(raw):
    assert refused(user(image_part(data_url(raw)))).code == media.UNSUPPORTED_IMAGE_TYPE


def test_a_zero_sized_image_is_refused():
    assert refused(user(image_part(data_url(png(0, 10))))).code == media.UNSUPPORTED_IMAGE_TYPE


def test_five_images_are_too_many_even_when_each_is_fine():
    one = image_part(data_url(png(2, 2)))

    assert len(media.validate_images(user(*[one] * media.MAX_IMAGES))) == media.MAX_IMAGES
    assert refused(user(*[one] * (media.MAX_IMAGES + 1))).code == media.TOO_MANY_IMAGES


def test_the_count_is_over_all_messages_and_is_checked_before_any_image_is_read():
    one = image_part(data_url(png(2, 2)))
    bad = image_part("https://example.com/a.png")
    messages = [{"role": "user", "content": [one, one, one]}, {"role": "user", "content": [one, one, bad]}]

    assert refused(messages).code == media.TOO_MANY_IMAGES


def test_the_size_limit_is_on_the_decoded_bytes():
    just_fits = png(10, 10, pad=media.MAX_IMAGE_BYTES - len(png(10, 10)))
    assert len(just_fits) == media.MAX_IMAGE_BYTES
    assert media.validate_images(user(image_part(data_url(just_fits))))

    assert refused(user(image_part(data_url(just_fits + b"x")))).code == media.IMAGE_TOO_LARGE


def test_a_huge_payload_is_turned_away_before_it_is_decoded(monkeypatch):
    def not_to_be_called(*a, **k):
        raise AssertionError("decoded a payload that was already too large")

    monkeypatch.setattr(media.base64, "b64decode", not_to_be_called)
    url = "data:image/png;base64," + "A" * (media.MAX_IMAGE_BYTES * 2)

    assert refused(user(image_part(url))).code == media.IMAGE_TOO_LARGE


def test_the_error_message_is_a_sentence_for_a_person():
    error = refused(user(image_part("https://example.com/a.png")))

    assert str(error) == error.message and error.message.endswith(".")
    assert "example.com" not in error.message             # what the client sent is not echoed back


def test_every_code_the_client_may_see_is_distinct():
    codes = [media.TOO_MANY_IMAGES, media.IMAGE_TOO_LARGE, media.UNSUPPORTED_IMAGE_TYPE, media.REMOTE_IMAGE_URL,
             media.IMAGE_INPUT_UNAVAILABLE]

    assert len(set(codes)) == len(codes)


# --- logging ------------------------------------------------------------------------------

def test_describe_gives_a_size_and_a_short_fingerprint_and_never_the_bytes():
    raw = png(640, 480, pad=13)
    (info,) = media.validate_images(user(image_part(data_url(raw))))
    line = media.describe(info)

    assert line.startswith("[image 640x480 sha256:") and len(info.sha256) == 64
    assert line == f"[image 640x480 sha256:{info.sha256[:8]}]"
    assert base64.b64encode(raw).decode()[:16] not in line


# --- message content ----------------------------------------------------------------------

def test_content_text_of_a_string_is_the_string_and_of_parts_the_text_parts():
    assert media.content_text("hi") == "hi"
    assert media.content_text(None) == ""
    assert media.content_text([{"type": "text", "text": "a"}, image_part("x"), {"type": "text", "text": "b"}]) == "a\nb"
    assert media.content_text([{"type": "text"}, "junk", 3]) == ""


def test_images_are_counted_in_parts_only():
    assert media.image_count("data:image/png;base64,AAAA") == 0
    assert media.image_count([image_part("x"), {"type": "text", "text": "t"}, image_part("y")]) == 2
    assert media.has_images([{"role": "user", "content": "hi"}, {"role": "user", "content": [image_part("x")]}])
    assert not media.has_images([{"role": "user", "content": "hi"}]) and not media.has_images(None)
    assert not media.has_images(["not a message"])


def test_add_text_keeps_the_images_and_the_shape():
    assert media.add_text("body", "tail") == "bodytail"
    assert media.add_text("body", "head ", front=True) == "head body"
    assert media.add_text(None, "x") == "x"

    parts = [image_part("x")]
    assert media.add_text(parts, "t") == [image_part("x"), {"type": "text", "text": "t"}]
    assert media.add_text(parts, "t", front=True) == [{"type": "text", "text": "t"}, image_part("x")]
    assert parts == [image_part("x")]                       # the caller's list is not changed


def test_tail_content_cuts_the_text_and_keeps_the_images():
    assert media.tail_content("abcdefghij", 4, "...") == "...ghij"
    assert media.tail_content("abc", 0, "...") == "..."      # zero is nothing, not everything

    cut = media.tail_content([{"type": "text", "text": "abcdefghij"}, image_part("x"), {"type": "text", "text": "k"}], 3, "...")

    assert cut == [{"type": "text", "text": "...j\nk"}, image_part("x")]


# --- completion prompts and the published limits ---------------------------------------------------------

@pytest.mark.parametrize("prompt, plain", [
    ("def f(", True), ("", True), ([], True), ([1, 2, 3], True), (["a", 5], True), ([[1, 2], [3]], True),
    ([["a", "b"]], True), ([[]], True), ([["a", 1], []], True),
    ({"prompt_string": "x", "multimodal_data": ["AAAA"]}, False), (["a", {"prompt_string": "x"}], False),
    ([True], False), ([[1, True]], False), ([[1.5]], False), ([[["a"]]], False), ([[1], "a"], False),
    (None, False), (5, False), (1.5, False),
])
def test_a_completion_prompt_is_text_tokens_or_a_batch_of_them(prompt, plain):
    assert media.is_plain_prompt(prompt) is plain


def test_the_public_limits_are_the_validators_own_constants():
    limits = media.public_limits(30)

    assert limits == {"formats": ["png", "jpeg"], "max_images": 4, "max_bytes": 4 * 1024 * 1024, "max_side": 4096,
                      "max_pixels": 4096 * 4096, "per_hour": 30}
    assert media.public_limits(0)["per_hour"] == 0


def test_every_published_format_is_one_the_validator_returns_and_nothing_else_is():
    formats = {media.validate_images(user(image_part(data_url(raw, mime))))[0].format
               for raw, mime in ((png(4, 4), "image/png"), (jpeg(4, 4), "image/jpeg"))}

    assert formats == set(media.public_limits(1)["formats"])
