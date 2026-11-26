"""阵列模板匹配的插值、歧义、噪声权重及标定文件检查。"""

import copy
import json

import numpy as np
import pytest

from core.array_analysis import ArrayAnalysis, compensate_common_response, coverage_grid, spatial_differences
from models.frame import MV_STEP


def uniform(value):
    return np.full(16, value, dtype=float)


def make_engine():
    engine = ArrayAnalysis()
    engine.set_baseline([uniform(99), uniform(100), uniform(101)])
    engine.add_template(0, [uniform(100)])
    engine.add_template(1, [uniform(98)])
    return engine


def test_signed_delta_and_weighted_interpolation():
    result = make_engine().analyze(uniform(99.5))
    np.testing.assert_array_equal(result.delta, uniform(-0.5))
    assert result.position == pytest.approx(0.25)
    assert result.range == pytest.approx((0.25, 0.25))
    assert result.score == pytest.approx(0)
    assert not result.rejected
    assert not result.ambiguous


def test_plateau_does_not_invent_position_and_keeps_agreed_coverage():
    engine = make_engine()
    engine.add_template(-1, [uniform(100)])
    engine.add_template(2, [uniform(98)])
    empty = engine.analyze(uniform(100))
    assert empty.position is None
    assert empty.ambiguous
    assert empty.range == (-1, 0)
    np.testing.assert_array_equal(coverage_grid(empty.range, "left"), np.zeros((4, 4)))
    covered = engine.analyze(uniform(98))
    assert covered.position is None
    assert covered.range == (1, 2)
    grid = coverage_grid(covered.range, "left")
    assert np.isnan(grid[:, 1]).all()
    np.testing.assert_array_equal(grid[:, 0], np.ones(4))


def test_disjoint_equal_responses_report_both_aliases():
    engine = make_engine()
    engine.add_template(2, [uniform(100)])
    result = engine.analyze(uniform(100))
    assert result.position is None
    assert result.range == (0, 2)
    assert result.ranges == ((0, 0), (2, 2))


def test_residual_rejects_without_hiding_delta():
    engine = make_engine()
    frame = uniform(99)
    frame[:8] -= 1
    frame[8:] += 1
    assert engine.analyze(frame).position == pytest.approx(0.5)
    result = engine.analyze(frame, threshold=1)
    assert result.score == pytest.approx(1 / MV_STEP)
    assert result.rejected
    assert result.position is None
    assert result.range is None
    assert result.candidate_range == pytest.approx((0.5, 0.5))
    np.testing.assert_array_equal(result.delta, frame - 100)


def test_session_baseline_updates_observations_and_normalizes_new_templates():
    engine = make_engine()
    original = engine.to_dict()
    shift = np.arange(16, dtype=float) + 4
    live_frame = uniform(99) + shift
    assert engine.analyze(live_frame, threshold=3).rejected
    engine.set_session_baseline([uniform(100) + shift])
    assert engine.to_dict() == original
    assert engine.analyze(live_frame, threshold=3).position == pytest.approx(0.5)
    engine.add_template(2, uniform(97) + shift)
    assert engine.to_dict()["templates"][-1]["frames"] == [uniform(97).tolist()]
    engine.clear_session_baseline()
    assert engine.analyze(uniform(97), threshold=3).position == pytest.approx(2)


def test_common_compensation_does_not_create_response_on_weak_reference_channel():
    engine = make_engine()
    channels = np.arange(16).reshape(4, 4)
    for position in (0.5, 1.0, 2.0):
        coverage = coverage_grid((position, position), "bottom")
        attenuation = position * 20 + coverage * 50
        attenuation[0, 0] = 0
        engine.add_template(position, (100 - attenuation).ravel())
    gains = engine.common_response_gains(channels, "bottom")
    corrected, common, count = compensate_common_response(attenuation, coverage, gains)
    assert common == 40
    assert count == 8
    np.testing.assert_allclose(corrected, coverage * 50)
    unknown, common, count = compensate_common_response(attenuation, np.ones((4, 4)), gains)
    assert common is None
    assert count == 0
    assert np.isnan(unknown).all()


def test_pooled_variance_excludes_pose_change_and_has_floor():
    engine = ArrayAnalysis()
    engine.set_baseline(uniform(100))
    engine.add_template(0, [uniform(99), uniform(101)])
    engine.add_template(1, [uniform(79), uniform(81)])
    np.testing.assert_array_equal(engine.pooled_variance, uniform(2))
    assert engine.add_template(1, [uniform(80)])
    np.testing.assert_array_equal(engine.pooled_variance, uniform(2))
    assert engine.remove_template(0)
    assert not engine.has_model
    np.testing.assert_array_equal(engine.variance, uniform(MV_STEP ** 2))
    assert not engine.remove_template(9)


def test_weights_reduce_influence_of_noisy_channel_and_prevent_extrapolation():
    engine = ArrayAnalysis()
    engine.set_baseline(uniform(0))
    for position in (0, 1):
        frames = np.zeros((2, 16))
        frames[:, 0] = position
        frames[:, 1] = [position - 10, position + 10]
        engine.add_template(position, frames)
    frame = uniform(0)
    frame[:2] = [0.25, 0.75]
    assert engine.analyze(frame).position == pytest.approx(0.25, abs=0.001)
    frame[:2] = [2, 2]
    assert engine.analyze(frame).position == 1


def test_extreme_finite_input_fails_without_nonfinite_output():
    engine = ArrayAnalysis.from_dict({"baseline": uniform(1e308).tolist()})
    with pytest.raises(ValueError):
        engine.analyze(uniform(-1e308))
    with pytest.raises(ValueError):
        spatial_differences(np.tile([1e308, -1e308, 1e308, -1e308], (4, 1)))
    with pytest.raises(ValueError):
        coverage_grid(np.array(1), "left")


def test_baseline_reset_and_invalid_mutation_are_atomic():
    engine = make_engine()
    prior = engine.to_dict()
    with pytest.raises(ValueError):
        engine.set_baseline([uniform(0), uniform(np.nan)])
    with pytest.raises(ValueError):
        engine.add_template(3, [uniform(1), uniform(np.inf)])
    assert engine.to_dict() == prior
    engine.set_baseline(uniform(101))
    assert engine.positions == ()
    assert not engine.has_model
    np.testing.assert_array_equal(engine.analyze(uniform(100)).delta, uniform(-1))
    assert np.isnan(ArrayAnalysis().analyze(uniform(100)).delta).all()


def test_profile_round_trip_and_previous_payload():
    engine = make_engine()
    payload = json.loads(json.dumps(engine.to_dict(), allow_nan=False))
    restored = ArrayAnalysis.from_dict(payload)
    assert restored.positions == (0, 1)
    assert restored.analyze(uniform(99)).position == pytest.approx(0.5)
    payload["samples"] = payload.pop("templates")
    payload.pop("version")
    assert ArrayAnalysis.from_dict(payload).positions == (0, 1)


@pytest.mark.parametrize("change", [
    lambda data: data.update(baseline=[0] * 15),
    lambda data: data.update(baseline=[None] * 16),
    lambda data: data.update(noise_floor_mv=0),
    lambda data: data.update(version=True),
    lambda data: data["templates"].append(copy.deepcopy(data["templates"][0])),
    lambda data: data["templates"][0].update(frames=[]),
    lambda data: data["templates"][0].update(frames=[[0] * 15]),
    lambda data: data["templates"][0].update(position=float("nan")),
])
def test_malformed_profile_is_rejected(change):
    payload = make_engine().to_dict()
    change(payload)
    with pytest.raises(ValueError):
        ArrayAnalysis.from_dict(payload)


def test_grid_orientation_and_missing_cells():
    horizontal, vertical = spatial_differences(np.arange(16).reshape(4, 4))
    np.testing.assert_array_equal(horizontal, np.ones((4, 3)))
    np.testing.assert_array_equal(vertical, np.full((3, 4), 4))
    np.testing.assert_array_equal(coverage_grid((4, 5), "bottom"), np.ones((4, 4)))
    left = coverage_grid((0.5, 0.5), "left")
    right = coverage_grid((0.5, 0.5), "right")
    top = coverage_grid((0.5, 0.5), "top")
    np.testing.assert_array_equal(left[:, 0], np.full(4, 0.5))
    np.testing.assert_array_equal(right, np.fliplr(left))
    np.testing.assert_array_equal(top, left.T)
    assert np.isnan(coverage_grid(None, "left")).all()
    with pytest.raises(ValueError):
        coverage_grid((0, 1), "unknown")
    with pytest.raises(ValueError):
        make_engine().analyze(uniform(99), threshold=float("nan"))
