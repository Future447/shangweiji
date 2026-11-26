"""
统一测量快照与衰减公式单元测试（core/measurement.py）。

验收点（任务书 八-1/2/3）：
- 基线 1000 / 当前 800  → 衰减量 +200 mV、检波电压下降比例 +20%
  （名称是"检波电压下降比例"，不是"峰峰值衰减率"）
- 基线 1000 / 当前 1050 → 衰减量 -50 mV、-5%
  不得取绝对值、不得截零、不得翻转
- 基线缺失 / 近零(<1.0 mV) / 负值 / 当前帧无效 / 饱和 / 断流
  → 按各量有效条件分别给出 None（UI 显示 "—"），绝不产生 NaN/Inf/
    巨大伪百分比
- 回放覆盖：CSV 保存值直通（None=未知），实时帧到达立即失效
- 本文件全部"直接设置 filtered_mv"，不经过 median/EMA 收敛过程
  （端到端收敛测试见 test_calibration_flow.py / test_demo_flow.py）
"""

import math
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from core.measurement import (
    ATTENUATION_FORMULA_ID,
    MIN_VALID_BASELINE_MV,
    MeasurementSnapshot,
    attenuation_for_state,
    build_snapshot,
    compute_attenuation_mv,
    compute_detector_drop_pct,
    drop_pct_for_state,
    filtered_for_state,
    sample_for_state,
)
from models.channel_state import ChannelState, ChannelStatus


def _state(
    baseline=None,
    filtered=None,
    sample=0.0,
    status=ChannelStatus.OK,
    sample_count=1,
) -> ChannelState:
    """直接构造 ChannelState（不经过滤波收敛）。"""
    st = ChannelState(channel=0)
    st.baseline_mv = baseline
    st.filtered_mv = filtered
    st.sample_mv = sample
    st.last_status = status
    st.sample_count = sample_count
    return st


class TestFormulaConstants(unittest.TestCase):
    def test_formula_id_stable(self):
        """公式版本标识必须存在且写明语义（CSV 追溯用）。"""
        self.assertTrue(
            ATTENUATION_FORMULA_ID.startswith(
                "attenuation_mv=baseline_mv-filtered_mv"
            )
        )

    def test_min_valid_baseline_documented(self):
        """百分比有效下限 = 1.0 mV（8 LSB；数值有效性下限，非硬件合格标准）。"""
        self.assertEqual(MIN_VALID_BASELINE_MV, 1.0)


class TestComputeAttenuation(unittest.TestCase):
    def test_plate_inserted_drop(self):
        """基线 1000 / 当前 800 → +200 mV（放入钢板的预期方向）。"""
        self.assertAlmostEqual(compute_attenuation_mv(1000.0, 800.0), 200.0)

    def test_signal_above_baseline_negative(self):
        """基线 1000 / 当前 1050 → -50 mV，不得取绝对值或截零。"""
        att = compute_attenuation_mv(1000.0, 1050.0)
        self.assertIsNotNone(att)
        self.assertLess(att, 0.0)
        self.assertAlmostEqual(att, -50.0)

    def test_missing_inputs_none(self):
        self.assertIsNone(compute_attenuation_mv(None, 800.0))
        self.assertIsNone(compute_attenuation_mv(1000.0, None))
        self.assertIsNone(compute_attenuation_mv(None, None))

    def test_non_finite_none(self):
        self.assertIsNone(compute_attenuation_mv(float("nan"), 800.0))
        self.assertIsNone(compute_attenuation_mv(1000.0, float("inf")))
        self.assertIsNone(compute_attenuation_mv(float("inf"), float("inf")))


class TestComputeDropPct(unittest.TestCase):
    def test_drop_20_percent(self):
        """200 / 1000 × 100 = +20%。"""
        self.assertAlmostEqual(
            compute_detector_drop_pct(200.0, 1000.0), 20.0
        )

    def test_negative_percent_kept(self):
        """-50 / 1000 × 100 = -5%（信号升高，负值如实保留）。"""
        pct = compute_detector_drop_pct(-50.0, 1000.0)
        self.assertIsNotNone(pct)
        self.assertAlmostEqual(pct, -5.0)

    def test_baseline_missing_none(self):
        self.assertIsNone(compute_detector_drop_pct(200.0, None))

    def test_baseline_near_zero_none(self):
        """基线 0.5 mV（< 1.0 mV 有效下限）→ None，不得产生巨大伪百分比。"""
        self.assertIsNone(compute_detector_drop_pct(0.4, 0.5))
        # 恰在下限之下
        self.assertIsNone(
            compute_detector_drop_pct(0.5, MIN_VALID_BASELINE_MV - 1e-9)
        )

    def test_baseline_zero_none(self):
        """零基线 → None（绝不除零产生 NaN/Inf）。"""
        self.assertIsNone(compute_detector_drop_pct(1.0, 0.0))

    def test_baseline_negative_none(self):
        """负基线 → None（百分比无物理意义），衰减量本身仍可为负值。"""
        self.assertIsNone(compute_detector_drop_pct(-900.0, -100.0))

    def test_baseline_exactly_at_floor_ok(self):
        """基线恰为 1.0 mV（=下限）→ 可计算。"""
        self.assertAlmostEqual(
            compute_detector_drop_pct(0.125, MIN_VALID_BASELINE_MV), 12.5
        )

    def test_attenuation_none_propagates(self):
        self.assertIsNone(compute_detector_drop_pct(None, 1000.0))

    def test_no_nan_or_inf_ever(self):
        """任意病态输入组合都不得产生 NaN/Inf。"""
        cases = [
            (None, None), (0.0, 0.0), (1e-12, 1e-12),
            (float("nan"), 1000.0), (100.0, float("nan")),
            (float("inf"), 1000.0), (100.0, float("inf")),
            (-1e9, -1e9), (4096.0, 0.0001),
        ]
        for att, base in cases:
            pct = compute_detector_drop_pct(att, base)
            if pct is not None:
                self.assertTrue(
                    math.isfinite(pct),
                    f"drop_pct({att}, {base}) 产生非有限值 {pct}",
                )


class TestAttenuationForState(unittest.TestCase):
    """状态门槛：无效/饱和/断流/无数据的当前帧不产出衰减值。"""

    def test_ok_status(self):
        st = _state(baseline=1000.0, filtered=800.0,
                    status=ChannelStatus.OK)
        self.assertAlmostEqual(attenuation_for_state(st, True), 200.0)
        self.assertAlmostEqual(drop_pct_for_state(st, True), 20.0)

    def test_not_calibrated_status(self):
        """未标定：基线 None → 公式自然给 None（状态本身允许计算）。"""
        st = _state(baseline=None, filtered=800.0,
                    status=ChannelStatus.NOT_CALIBRATED)
        self.assertIsNone(attenuation_for_state(st, True))
        self.assertIsNone(drop_pct_for_state(st, True))

    def test_saturated_frame_none(self):
        st = _state(baseline=1000.0, filtered=800.0,
                    status=ChannelStatus.SATURATED)
        self.assertIsNone(attenuation_for_state(st, True))
        self.assertIsNone(drop_pct_for_state(st, True))

    def test_invalid_frame_none(self):
        st = _state(baseline=1000.0, filtered=800.0,
                    status=ChannelStatus.INVALID)
        self.assertIsNone(attenuation_for_state(st, True))

    def test_no_data_none(self):
        st = _state(baseline=1000.0, filtered=800.0,
                    status=ChannelStatus.NO_DATA, sample_count=0)
        self.assertIsNone(attenuation_for_state(st, True))

    def test_frame_invalid_flag_none(self):
        """断流（frame_valid=False）：即使状态 OK 也不产出当前派生值。"""
        st = _state(baseline=1000.0, filtered=800.0,
                    status=ChannelStatus.OK)
        self.assertIsNone(attenuation_for_state(st, False))
        self.assertIsNone(drop_pct_for_state(st, False))

    def test_negative_signal_rise_passthrough(self):
        st = _state(baseline=1000.0, filtered=1050.0,
                    status=ChannelStatus.OK)
        self.assertAlmostEqual(attenuation_for_state(st, True), -50.0)
        self.assertAlmostEqual(drop_pct_for_state(st, True), -5.0)


class TestReplayOverrides(unittest.TestCase):
    def test_replay_value_passthrough(self):
        """回放：直接使用 CSV 保存值，不重算、不二次翻转。"""
        st = _state(baseline=1000.0, filtered=1050.0,
                    status=ChannelStatus.OK)
        st.set_replay_derived(-50.0, -5.0)
        self.assertAlmostEqual(attenuation_for_state(st, True), -50.0)
        self.assertAlmostEqual(drop_pct_for_state(st, True), -5.0)

    def test_replay_invalid_frame_gated(self):
        """回放无效帧不得绕过有效性门槛（P2 修复）。

        旧 CSV 在无效帧写入了上一帧的残留衰减/滤波值；回放这些
        文件时，raw_valid=0 的行必须把残留值按无效处理（显示
        "—"），不得显示成当前有效值。
        """
        st = _state(baseline=1000.0, filtered=1050.0,
                    status=ChannelStatus.OK)
        st.set_replay_derived(-50.0, -5.0)
        self.assertIsNone(attenuation_for_state(st, False))
        self.assertIsNone(drop_pct_for_state(st, False))
        self.assertIsNone(filtered_for_state(st, False))
        self.assertIsNone(sample_for_state(st, False))
        # 有效行仍按保存值原样通过
        self.assertAlmostEqual(attenuation_for_state(st, True), -50.0)
        self.assertAlmostEqual(filtered_for_state(st, True), 1050.0)

    def test_replay_none_is_unknown_not_zero(self):
        """回放保存值为空 → None（未知），不得默认为 0。"""
        st = _state(baseline=1000.0, filtered=800.0,
                    status=ChannelStatus.OK)
        st.set_replay_derived(None, None)
        self.assertIsNone(attenuation_for_state(st, True))
        self.assertIsNone(drop_pct_for_state(st, True))

    def test_live_frame_clears_overrides(self):
        """实时帧到达：回放覆盖立即失效，回到公式语义。"""
        st = _state(baseline=1000.0, filtered=800.0,
                    status=ChannelStatus.OK)
        st.set_replay_derived(-50.0, -5.0)
        st.update_mv(800.0)
        self.assertFalse(st.use_replay_derived)
        self.assertAlmostEqual(attenuation_for_state(st, True), 200.0)


class TestBuildSnapshot(unittest.TestCase):
    def test_full_snapshot(self):
        st = _state(baseline=1000.0, filtered=800.0, sample=810.0,
                    status=ChannelStatus.OK)
        snap = build_snapshot(st, frame_valid=True, detector_divider=2.0)
        self.assertIsInstance(snap, MeasurementSnapshot)
        self.assertAlmostEqual(snap.attenuation_mv, 200.0)
        self.assertAlmostEqual(snap.detector_drop_pct, 20.0)
        self.assertAlmostEqual(snap.baseline_mv, 1000.0)
        self.assertAlmostEqual(snap.filtered_mv, 800.0)
        self.assertAlmostEqual(snap.sample_mv, 810.0)
        self.assertAlmostEqual(snap.detector_vout_mv, 1620.0)
        # 旧诊断 Δ = filtered - baseline（与衰减量符号相反）
        self.assertAlmostEqual(snap.delta_legacy_mv, -200.0)
        self.assertTrue(snap.has_current_measurement)

    def test_invalid_frame_keeps_baseline_hides_current(self):
        """当前帧无效：基线（历史标定参数）保留显示，当前派生值 None。"""
        st = _state(baseline=1000.0, filtered=800.0, sample=800.0,
                    status=ChannelStatus.INVALID)
        snap = build_snapshot(st, frame_valid=False)
        self.assertAlmostEqual(snap.baseline_mv, 1000.0)
        self.assertIsNone(snap.attenuation_mv)
        self.assertIsNone(snap.detector_drop_pct)
        self.assertIsNone(snap.delta_legacy_mv)
        self.assertFalse(snap.has_current_measurement)

    def test_no_samples_sample_none(self):
        """从未收到样本：sample 显示 None 而不是伪造的 0。"""
        st = _state(baseline=None, filtered=None, sample=0.0,
                    status=ChannelStatus.NO_DATA, sample_count=0)
        snap = build_snapshot(st, frame_valid=False)
        self.assertIsNone(snap.sample_mv)
        self.assertIsNone(snap.detector_vout_mv)

    def test_replay_snapshot_uses_overrides(self):
        st = _state(baseline=1000.0, filtered=800.0, sample=800.0,
                    status=ChannelStatus.OK)
        st.set_replay_derived(-50.0, -5.0)
        snap = build_snapshot(st, frame_valid=True)
        self.assertAlmostEqual(snap.attenuation_mv, -50.0)
        self.assertAlmostEqual(snap.detector_drop_pct, -5.0)


class TestProcessorMeasurementEntry(unittest.TestCase):
    """ChannelProcessor.measurement() 是 UI 的统一入口。"""

    def test_processor_measurement_matches_formula(self):
        from core.channel_processor import ChannelProcessor
        proc = ChannelProcessor(enabled_channels=[0])
        st = proc.get(0)
        st.baseline_mv = 1000.0
        st.filtered_mv = 800.0
        st.sample_mv = 810.0
        st.sample_count = 1
        st.last_status = ChannelStatus.OK
        snap = proc.measurement(0, True)
        self.assertAlmostEqual(snap.attenuation_mv, 200.0)
        self.assertAlmostEqual(snap.detector_drop_pct, 20.0)
        # 同一帧、同一来源：再取一次结果一致（详情/热力图/CSV 同源）
        snap2 = proc.measurement(0, True)
        self.assertEqual(snap.attenuation_mv, snap2.attenuation_mv)
        self.assertEqual(snap.detector_drop_pct, snap2.detector_drop_pct)

    def test_invert_sign_does_not_flip_attenuation(self):
        """旧 invert_sign 绝不再翻转 attenuation_mv。"""
        from core.channel_processor import ChannelProcessor
        proc = ChannelProcessor(enabled_channels=[0])
        st = proc.get(0)
        st.baseline_mv = 1000.0
        st.filtered_mv = 1050.0
        st.sample_mv = 1050.0
        st.sample_count = 1
        st.last_status = ChannelStatus.OK
        proc.set_invert_sign(True)
        snap = proc.measurement(0, True)
        self.assertAlmostEqual(snap.attenuation_mv, -50.0)
        self.assertAlmostEqual(snap.detector_drop_pct, -5.0)
        # 旧诊断量仍受 invert_sign 影响（互不影响）
        self.assertAlmostEqual(proc.delta_signed(0), -50.0)


if __name__ == "__main__":
    unittest.main()
