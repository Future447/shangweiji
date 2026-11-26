"""
v3 毫伏协议解析器测试。

帧格式（权威 ../v2/PROTOCOL.md）：
    D,<scan>,<status>,<valid_mask>,<i2c_err>,mV,<16×mV 3位小数>\r\n
    - 固定 22 字段；parts[5] 必须为字面量 "mV"
    - 每个测量值固定 3 位小数，是 0.125 mV 的整数倍
    - 范围 [-4096.000, 4095.875]

覆盖：
- COM9 实机帧样本（首通道 424.625 精确解析）
- 22 字段严格校验（21/23 字段拒绝）
- mV 单位标记缺失/错误拒绝
- 3 位小数格式拒绝矩阵（整数/2位小数/4位小数/科学计数法/NaN/Inf）
- 边界值 -4096.000 / -0.125 / 0.000 / 0.125 / 1.000 / 4095.875
- 越界与非 0.125 整数倍拒绝
- 半包 / 多帧粘包 / CR / LF / CRLF
- 空行 / `#` 诊断 / 非数字
- 超长缓存 / reset / valid_mask
"""

import unittest

from core.protocol_parser import FrameParser, FrameKind
from models.frame import (
    DFrame, EFrame, FrameParseError, MAX_CHANNEL_COUNT,
    MV_MAX, MV_MIN, format_mv, is_valid_mv_value, mv_to_uv,
    parse_d_frame, parse_e_frame, parse_mv_field,
)


def make_d_line(
    scan=1,
    status=0,
    mask=0xFFFF,
    i2c=0,
    samples=None,
    unit="mV",
) -> str:
    if samples is None:
        samples = [100.0 + i * 0.125 for i in range(16)]
    parts = ["D", str(scan), str(status), str(mask), str(i2c), unit]
    for s in samples:
        if isinstance(s, str):
            parts.append(s)
        else:
            parts.append(format_mv(s))
    return ",".join(parts) + "\r\n"


def make_e_line(s=0, ia=1, ra=0, te=0) -> str:
    return f"E,INIT,{s},{ia},{ra},{te}\r\n"


# COM9 实机捕获样本（2026-09，STM32 v3 固件，~3.48 fps）
COM9_SAMPLE = (
    "D,10464,0,65535,0,mV,"
    "424.625,306.250,412.500,388.375,"
    "401.125,395.750,410.000,378.625,"
    "421.375,305.500,419.875,399.000,"
    "405.250,391.500,408.375,382.750\r\n"
)


class TestDFrameParsing(unittest.TestCase):
    def test_com9_real_frame_first_channel_exact(self):
        """实机样本：首通道必须精确解析为 424.625 mV。"""
        df = parse_d_frame(COM9_SAMPLE)
        self.assertEqual(df.scan_number, 10464)
        self.assertEqual(df.scan_status, 0)
        self.assertEqual(df.valid_mask, 65535)
        self.assertEqual(df.i2c_error_count, 0)
        self.assertEqual(df.unit, "mV")
        self.assertEqual(df.samples_mv[0], 424.625)
        self.assertEqual(df.samples_mv[1], 306.250)
        # 精确到 µV 整数，无浮点漂移
        self.assertEqual(mv_to_uv(df.samples_mv[0]), 424625)
        self.assertEqual(df.valid_count(), 16)

    def test_424_625_is_exact_binary_float(self):
        """424.625 = 424 + 5/8，二进制浮点精确表示。"""
        v = parse_mv_field("424.625", 0)
        self.assertEqual(v, 424.625)
        self.assertEqual(v * 8.0, 3397.0)

    def test_basic_16ch(self):
        line = make_d_line()
        df = parse_d_frame(line)
        self.assertEqual(len(df.samples_mv), 16)
        self.assertEqual(df.valid_count(), 16)
        self.assertEqual(df.samples_mv[0], 100.0)
        self.assertEqual(df.samples_mv[1], 100.125)

    def test_basic_4ch_mask(self):
        samples = [100.0, 200.0, 300.0, 400.0] + [0.0] * 12
        line = make_d_line(mask=0x000F, samples=samples)
        df = parse_d_frame(line)
        self.assertTrue(df.is_valid(0))
        self.assertTrue(df.is_valid(3))
        self.assertFalse(df.is_valid(4))
        self.assertFalse(df.is_valid(15))
        self.assertEqual(df.valid_count(), 4)

    def test_boundary_min(self):
        line = make_d_line(mask=1, samples=[-4096.0] + [0.0] * 15)
        df = parse_d_frame(line)
        self.assertEqual(df.samples_mv[0], -4096.0)
        self.assertEqual(df.samples_mv[0], MV_MIN)

    def test_boundary_max(self):
        line = make_d_line(mask=1, samples=[4095.875] + [0.0] * 15)
        df = parse_d_frame(line)
        self.assertEqual(df.samples_mv[0], 4095.875)
        self.assertEqual(df.samples_mv[0], MV_MAX)

    def test_boundary_fractions(self):
        samples = [-0.125, 0.0, 0.125, 1.0] + [0.0] * 12
        line = make_d_line(samples=samples)
        df = parse_d_frame(line)
        self.assertEqual(df.samples_mv[0], -0.125)
        self.assertEqual(df.samples_mv[1], 0.0)
        self.assertEqual(df.samples_mv[2], 0.125)
        self.assertEqual(df.samples_mv[3], 1.0)

    def test_field_format_strings(self):
        """固件风格字符串：0.000 / -0.125 / 1.000 / -4096.000。"""
        samples = ["0.000", "-0.125", "1.000", "-4096.000"] + ["0.000"] * 12
        line = make_d_line(samples=samples)
        df = parse_d_frame(line)
        self.assertEqual(df.samples_mv[0], 0.0)
        self.assertEqual(df.samples_mv[1], -0.125)
        self.assertEqual(df.samples_mv[2], 1.0)
        self.assertEqual(df.samples_mv[3], -4096.0)

    # ---------------- 拒绝矩阵 ----------------
    def test_reject_21_fields_old_v2(self):
        """旧 v2 21 字段 raw count 帧必须被拒绝。"""
        line = "D,1,0,65535,0," + ",".join(["12000"] * 16) + "\r\n"
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_23_fields(self):
        line = make_d_line().rstrip("\r\n") + ",1.000\r\n"
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_missing_mv_marker(self):
        """parts[5] 不是 mV → 拒绝。"""
        line = "D,1,0,65535,0,raw," + ",".join(["1.000"] * 16) + "\r\n"
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_wrong_unit_token(self):
        line = make_d_line(unit="V")
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_plain_integer(self):
        line = make_d_line(mask=1, samples=["12000"] + ["0.000"] * 15)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_two_decimals(self):
        line = make_d_line(mask=1, samples=["424.62"] + ["0.000"] * 15)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_four_decimals(self):
        line = make_d_line(mask=1, samples=["424.6250"] + ["0.000"] * 15)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_no_fraction(self):
        line = make_d_line(mask=1, samples=["424."] + ["0.000"] * 15)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_scientific(self):
        line = make_d_line(mask=1, samples=["4.2e2"] + ["0.000"] * 15)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_nan_inf(self):
        for bad in ("nan", "NaN", "inf", "-inf", "Infinity"):
            line = make_d_line(mask=1, samples=[bad] + ["0.000"] * 15)
            with self.assertRaises(FrameParseError, msg=bad):
                parse_d_frame(line)

    def test_reject_out_of_range_high(self):
        line = make_d_line(mask=1, samples=["4096.000"] + ["0.000"] * 15)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_out_of_range_low(self):
        line = make_d_line(mask=1, samples=["-4096.125"] + ["0.000"] * 15)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_reject_non_step_multiple(self):
        """不是 0.125 mV 整数倍 → 拒绝（1.001 / 0.010 / 424.626）。"""
        for bad in ("1.001", "0.010", "424.626", "-0.126"):
            line = make_d_line(mask=1, samples=[bad] + ["0.000"] * 15)
            with self.assertRaises(FrameParseError, msg=bad):
                parse_d_frame(line)

    def test_reject_plus_sign_and_spaces(self):
        for bad in ("+1.000", " 1.000", "1.000 "):
            line = make_d_line(mask=1, samples=[bad] + ["0.000"] * 15)
            with self.assertRaises(FrameParseError, msg=repr(bad)):
                parse_d_frame(line)

    # ---------------- 元数据 ----------------
    def test_scan_number_max(self):
        line = make_d_line(scan=0xFFFFFFFF, mask=0)
        df = parse_d_frame(line)
        self.assertEqual(df.scan_number, 0xFFFFFFFF)

    def test_scan_number_overflow(self):
        with self.assertRaises(FrameParseError):
            parse_d_frame(make_d_line(scan=0x100000000, mask=0))

    def test_non_numeric_metadata(self):
        line = make_d_line().replace("D,1,", "D,abc,", 1)
        with self.assertRaises(FrameParseError):
            parse_d_frame(line)

    def test_valid_mask_all_zero(self):
        """valid_mask=0：帧本身合法，但所有通道 is_valid=False。"""
        line = make_d_line(mask=0)
        df = parse_d_frame(line)
        self.assertEqual(df.valid_count(), 0)
        for ch in range(16):
            self.assertFalse(df.is_valid(ch))

    def test_dframe_immutable_validation(self):
        """直接构造非法 DFrame 会被 __post_init__ 拒绝。"""
        with self.assertRaises(ValueError):
            DFrame(
                scan_number=1, scan_status=0, valid_mask=0xFFFF,
                i2c_error_count=0, samples_mv=(5000.0,) * 16,
            )
        with self.assertRaises(ValueError):
            DFrame(
                scan_number=1, scan_status=0, valid_mask=0xFFFF,
                i2c_error_count=0, samples_mv=(1.001,) * 16,
            )

    def test_is_valid_mv_value(self):
        self.assertTrue(is_valid_mv_value(0.0))
        self.assertTrue(is_valid_mv_value(-4096.0))
        self.assertTrue(is_valid_mv_value(4095.875))
        self.assertFalse(is_valid_mv_value(4096.0))
        self.assertFalse(is_valid_mv_value(1.001))
        self.assertFalse(is_valid_mv_value(float("nan")))

    def test_format_mv_roundtrip(self):
        for v in (-4096.0, -0.125, 0.0, 0.125, 1.0, 424.625, 4095.875):
            self.assertEqual(parse_mv_field(format_mv(v), 0), v)


class TestEFrameParsing(unittest.TestCase):
    def test_basic(self):
        ef = parse_e_frame(make_e_line(s=0, ia=5, ra=2, te=10))
        self.assertEqual(ef.subtype, "INIT")
        self.assertEqual(ef.init_status, 0)
        self.assertEqual(ef.init_attempts, 5)
        self.assertEqual(ef.recover_attempts, 2)
        self.assertEqual(ef.total_errors, 10)

    def test_unknown_subtype(self):
        with self.assertRaises(FrameParseError):
            parse_e_frame("E,OTHER,0,0,0,0\r\n")

    def test_wrong_field_count(self):
        with self.assertRaises(FrameParseError):
            parse_e_frame("E,INIT,0,0,0\r\n")


class TestParserStream(unittest.TestCase):
    def setUp(self):
        self.p = FrameParser()

    def test_single_frame_crlf(self):
        out = self.p.feed(make_d_line().encode("ascii"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.D)
        self.assertIsInstance(out[0].d_frame, DFrame)

    def test_single_frame_lf(self):
        line = make_d_line().replace("\r\n", "\n")
        out = self.p.feed(line.encode("ascii"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.D)

    def test_single_frame_cr_only(self):
        line = make_d_line().replace("\r\n", "\r")
        out = self.p.feed(line.encode("ascii"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.D)

    def test_split_two_packets(self):
        b = make_d_line().encode("ascii")
        mid = len(b) // 2
        out1 = self.p.feed(b[:mid])
        self.assertEqual(len(out1), 0)
        out2 = self.p.feed(b[mid:])
        self.assertEqual(len(out2), 1)
        self.assertEqual(out2[0].kind, FrameKind.D)

    def test_split_mid_value(self):
        """在 mV 值正中间拆包也不能出错。"""
        b = COM9_SAMPLE.encode("ascii")
        # 找 "424.625" 中间
        idx = b.index(b"424") + 4
        out1 = self.p.feed(b[:idx])
        self.assertEqual(len(out1), 0)
        out2 = self.p.feed(b[idx:])
        self.assertEqual(len(out2), 1)
        self.assertEqual(out2[0].d_frame.samples_mv[0], 424.625)

    def test_two_frames_sticky(self):
        line1 = make_d_line(scan=1)
        line2 = make_d_line(scan=2)
        out = self.p.feed((line1 + line2).encode("ascii"))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].d_frame.scan_number, 1)
        self.assertEqual(out[1].d_frame.scan_number, 2)

    def test_empty_line_ignored(self):
        line = "\n\n" + make_d_line() + "\n\n"
        out = self.p.feed(line.encode("ascii"))
        d_outs = [o for o in out if o.kind == FrameKind.D]
        self.assertEqual(len(d_outs), 1)

    def test_diag_line(self):
        out = self.p.feed(b"# some diagnostic\r\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.DIAG)

    def test_unknown_line(self):
        out = self.p.feed(b"random garbage text here\r\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.UNKNOWN)
        self.assertEqual(self.p.error_count, 1)

    def test_bad_mv_value_returns_unknown_not_crash(self):
        """非法 mV 值 → UNKNOWN + error_count，不抛异常。"""
        bad = "D,1,0,65535,0,mV," + ",".join(
            ["9999.999"] + ["0.000"] * 15
        ) + "\r\n"
        out = self.p.feed(bad.encode("ascii"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.UNKNOWN)
        self.assertIsNotNone(out[0].error)
        self.assertEqual(self.p.error_count, 1)

    def test_missing_mv_marker_in_stream(self):
        bad = "D,1,0,65535,0," + ",".join(["0.000"] * 16) + "\r\n"
        out = self.p.feed(bad.encode("ascii"))
        self.assertEqual(out[0].kind, FrameKind.UNKNOWN)

    def test_e_frame(self):
        out = self.p.feed(make_e_line().encode("ascii"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.E)
        self.assertIsInstance(out[0].e_frame, EFrame)

    def test_mixed_d_and_e(self):
        text = make_d_line() + make_e_line()
        out = self.p.feed(text.encode("ascii"))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].kind, FrameKind.D)
        self.assertEqual(out[1].kind, FrameKind.E)

    def test_diag_among_frames(self):
        text = (
            "# DIAG start\r\n" + make_d_line()
            + "# DIAG mid\r\n" + make_d_line(scan=2)
        )
        out = self.p.feed(text.encode("ascii"))
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0].kind, FrameKind.DIAG)
        self.assertEqual(out[1].kind, FrameKind.D)
        self.assertEqual(out[2].kind, FrameKind.DIAG)
        self.assertEqual(out[3].kind, FrameKind.D)

    def test_oversized_buffer_resets(self):
        garbage = b"GARBAGE" * 10000
        out = self.p.feed(garbage)
        self.assertEqual(len(out), 0)
        self.assertGreater(self.p.reset_count, 0)

    def test_parser_reset(self):
        self.p.feed(b"# partial")
        self.p.reset()
        self.assertEqual(self.p.reset_count, 1)
        out = self.p.feed(b"1234\r\n")
        self.assertEqual(len(out), 1)

    def test_flush_with_partial(self):
        self.p.feed(b"D,1,0,0,")
        out = self.p.flush()
        self.assertGreaterEqual(len(out), 1)
        self.assertEqual(out[0].kind, FrameKind.UNKNOWN)

    def test_garbage_then_legal(self):
        text = b"X" * 5000 + b"\r\n" + make_d_line().encode("ascii")
        out = self.p.feed(text)
        d_outs = [o for o in out if o.kind == FrameKind.D]
        self.assertGreaterEqual(len(d_outs), 1)

    def test_repeated_d_frames_no_buffer_growth(self):
        for _ in range(200):
            self.p.feed(make_d_line().encode("ascii"))
        self.assertLess(self.p.stats()["buffer_bytes"], 100)

    def test_diag_then_two_d_frames(self):
        text = "# marker\r\n" + make_d_line(scan=10) + make_d_line(scan=11)
        out = self.p.feed(text.encode("ascii"))
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1].d_frame.scan_number, 10)
        self.assertEqual(out[2].d_frame.scan_number, 11)

    def test_com9_stream_10_frames(self):
        """模拟实机流：10 帧粘包全部解析，无错误。"""
        stream = COM9_SAMPLE * 10
        out = self.p.feed(stream.encode("ascii"))
        self.assertEqual(len(out), 10)
        self.assertEqual(self.p.error_count, 0)
        self.assertEqual(self.p.d_frame_count, 10)
        for o in out:
            self.assertEqual(o.d_frame.samples_mv[0], 424.625)


if __name__ == "__main__":
    unittest.main()
