"""良品参考差分的噪声、位置、缺测及配置回归检查。"""

import copy
import json

import numpy as np
import pytest

from core.inspection import DEFAULT_INSPECTION_THRESHOLD, InspectionReference
from models.frame import MV_STEP


def full(value):
    return np.full(16, value, dtype=float)


def static_reference():
    return InspectionReference.fit([full(100), full(100), full(100), full(100), full(200)])


def test_robust_static_reference_preserves_signed_residual_and_hits():
    reference = static_reference()
    frame = full(100)
    frame[:3] = [101, 99, 100 + MV_STEP]
    result = reference.analyze(frame)
    np.testing.assert_array_equal(result.residual_mv[:3], [1, -1, MV_STEP])
    np.testing.assert_array_equal(result.scores[:3], [8, 8, 1])
    np.testing.assert_array_equal(result.hit_mask[:3], [True, True, False])
    assert result.quality == "ok"
    assert result.threshold == DEFAULT_INSPECTION_THRESHOLD
    assert not reference.has_positions
    assert reference.positions == ()


def test_noise_is_mad_in_mv_and_threshold_can_be_disabled():
    reference = InspectionReference.fit([full(99), full(100), full(101)])
    result = reference.analyze(full(101.4826022185056), threshold=None)
    np.testing.assert_allclose(result.scale_mv, full(1.482602218505602))
    np.testing.assert_allclose(result.scores, full(1))
    assert not result.hit_mask.any()


def test_dead_reference_and_invalid_current_channels_stay_unknown():
    frames = np.full((4, 16), 100.0)
    frames[:, 0] = np.nan
    frames[:2, 1] = np.nan
    reference = InspectionReference.fit(frames)
    assert not reference.valid_channels[:2].any()
    current = full(101)
    current[2] = np.nan
    current[3] = np.inf
    result = reference.analyze(current)
    assert result.quality == "partial"
    assert np.isnan(result.residual_mv[:4]).all()
    assert not result.hit_mask[:4].any()
    assert reference.analyze(full(np.nan)).quality == "invalid_frame"
    only_unreferenced = full(np.nan)
    only_unreferenced[0] = 100
    assert reference.analyze(only_unreferenced).quality == "reference_unavailable"
    with pytest.raises(ValueError):
        InspectionReference.fit(np.full((3, 16), np.nan))


def test_position_reference_sorts_repeats_and_refuses_extrapolation():
    positions = [2, 0, 2, 0, 2, 0]
    reference = InspectionReference.fit([full(120 if p == 2 else 100) for p in positions], positions)
    assert reference.has_positions
    assert reference.positions == (0, 2)
    np.testing.assert_array_equal(reference.analyze(full(111), 1).residual_mv, full(1))
    np.testing.assert_array_equal(reference.analyze(full(100), 0).residual_mv, full(0))
    for position in (-0.001, 2.001):
        result = reference.analyze(full(111), position)
        assert result.quality == "outside_reference_range"
        assert np.isnan(result.residual_mv).all()
        assert not result.hit_mask.any()
    assert reference.analyze(full(111)).quality == "position_required"


def test_single_per_bin_uses_floor_instead_of_spatial_weld_variance():
    reference = InspectionReference.fit([full(100), full(200), full(300)], [0, 1, 2])
    assert not reference.noise_supported.any()
    result = reference.analyze(full(251), 1.5)
    np.testing.assert_array_equal(result.reference_mv, full(250))
    np.testing.assert_array_equal(result.scale_mv, full(MV_STEP))
    np.testing.assert_array_equal(result.scores, full(8))


def test_pooled_repeat_noise_supports_single_bin_and_missing_neighbors_do_not_fill():
    frames = np.array([full(99), full(100), full(101), full(110), full(120)])
    frames[-1, 0] = np.nan
    reference = InspectionReference.fit(frames, [0, 0, 0, 1, 2])
    assert reference.noise_supported.all()
    np.testing.assert_allclose(reference.analyze(full(110), 1).scale_mv, full(1.482602218505602))
    result = reference.analyze(full(115), 1.5)
    assert np.isnan(result.residual_mv[0])
    assert result.quality == "partial"
    assert reference.analyze(full(120), 2).quality == "partial"


def test_single_coordinate_is_available_only_at_the_recorded_position():
    reference = InspectionReference.fit([full(100)] * 3, [1] * 3)
    assert reference.analyze(full(100), 1).quality == "ok"
    assert reference.analyze(full(100), 1.001).quality == "outside_reference_range"


def test_profile_roundtrip_is_json_finite_and_preserves_missing_points():
    frames = np.array([full(99), full(100), full(101), full(110)])
    frames[-1, 0] = np.nan
    reference = InspectionReference.fit(frames, [0, 0, 0, 1])
    payload = json.loads(json.dumps(reference.to_dict(), allow_nan=False))
    assert payload["centers_mv"][1][0] is None
    restored = InspectionReference.from_dict(payload)
    assert restored.positions == reference.positions
    np.testing.assert_allclose(restored.analyze(full(105), .5).scores, reference.analyze(full(105), .5).scores, equal_nan=True)
    assert restored.to_dict() == payload


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(version=True),
    lambda p: p.update(kind="array-analysis"),
    lambda p: p.update(noise_floor_mv=0),
    lambda p: p["centers_mv"][0].pop(),
    lambda p: p["centers_mv"][0].__setitem__(0, float("nan")),
    lambda p: p["centers_mv"][0].__setitem__(0, True),
    lambda p: p["scales_mv"][0].__setitem__(0, -1),
    lambda p: p["scales_mv"][0].__setitem__(0, None),
    lambda p: p["counts"][0].__setitem__(0, .5),
    lambda p: p["counts"][0].__setitem__(0, 0),
    lambda p: p["counts"][0].__setitem__(0, 2 ** 63),
    lambda p: p.update(positions=[float("inf")]),
])
def test_malformed_profile_is_rejected(mutate):
    payload = copy.deepcopy(static_reference().to_dict())
    mutate(payload)
    with pytest.raises(ValueError):
        InspectionReference.from_dict(payload)


def test_bad_input_and_position_vectors_are_rejected():
    with pytest.raises(ValueError):
        InspectionReference.fit([full(100)] * 2)
    with pytest.raises(ValueError):
        InspectionReference.fit([full(100)] * 3, [0, 1])
    with pytest.raises(ValueError):
        InspectionReference.fit([full(100)] * 3, [0, 1, np.nan])
    with pytest.raises(ValueError):
        static_reference().analyze(full(100), threshold=-1)
    with pytest.raises(ValueError):
        static_reference().analyze([100] * 15)
