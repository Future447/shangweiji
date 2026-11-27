"""Whole-frame compensation, signed local recovery and calibration safeguards."""

import copy
import json

import numpy as np
import pytest

from core.compensation import CompensationProfile
from models.frame import MV_STEP


def baseline():
    return np.arange(16, dtype=float) + 100.0


def local_templates(matrix, gains=None):
    gains = np.ones(16) if gains is None else np.asarray(gains)
    return {index: baseline() + (12 if index % 2 else -12) * matrix[:, index] + gains * (index - 8) for index in range(16)}


def test_default_common_background_and_sparse_signed_delta():
    profile = CompensationProfile(baseline())
    np.testing.assert_array_equal(profile.apply(baseline() + 30), np.zeros(16))
    local = np.zeros(16)
    local[[2, 7, 10]] = [4, -5, 8]
    np.testing.assert_array_equal(profile.apply(baseline() + 30 + local), local)
    assert profile.correction_mode == "common_only"
    # This is a spatial constraint: a uniform physical change is removed too.
    np.testing.assert_array_equal(profile.apply(baseline() - 12), np.zeros(16))


def test_measure_robust_finite_complete_and_defensive():
    frames = np.tile(baseline(), (5, 1))
    frames[-1] += 100
    measured = CompensationProfile.measure(frames)
    np.testing.assert_array_equal(measured, baseline())
    assert not measured.flags.writeable
    for bad in (frames[:2], frames[:, :15], baseline(), [[True] * 16] * 3):
        with pytest.raises(ValueError):
            CompensationProfile.measure(bad)
    frames[0, 0] = np.nan
    with pytest.raises(ValueError):
        CompensationProfile.measure(frames)


def test_gain_calibration_and_sparse_local_response():
    gains = np.linspace(0.6, 1.4, 16)
    profile = CompensationProfile(baseline(), templates={0: baseline() + np.eye(16)[0] * 8})
    updated = profile.with_uniform_background([baseline() + gains * 20] * 3)
    np.testing.assert_allclose(updated.common_gains, gains)
    assert updated.template_count == 0
    assert profile.template_count == 1
    local = np.zeros(16)
    local[5] = -4
    np.testing.assert_allclose(updated.apply(baseline() - gains * 50 + local), local, atol=1e-12)
    for background in (baseline() + MV_STEP, baseline() + np.r_[np.full(15, 3), -3], baseline() + np.r_[np.full(15, 3), MV_STEP]):
        with pytest.raises(ValueError):
            CompensationProfile.fit_common_gains(baseline(), background)


def test_partial_probe_calibration_remains_common_only():
    matrix = np.eye(16) + np.roll(np.eye(16), 1, axis=0) * 0.25
    templates = local_templates(matrix)
    profile = CompensationProfile(baseline(), templates={key: value for key, value in templates.items() if key != 15})
    local = matrix[:, 3] * 8
    np.testing.assert_allclose(profile.apply(baseline() + local + 40), local)
    assert profile.correction_mode == "common_only"
    assert profile.template_count == 15


def test_partial_templates_preserve_effective_fingerprint_until_coupling_engages():
    templates = local_templates(np.eye(16))
    empty = CompensationProfile(baseline())
    partial = CompensationProfile(baseline(), templates={index: templates[index] for index in range(15)})
    assert partial.fingerprint == empty.fingerprint
    assert len(partial.to_dict()["templates"]) == 15
    assert CompensationProfile.from_dict(partial.to_dict()).fingerprint == empty.fingerprint
    full = CompensationProfile(baseline(), templates=templates)
    assert full.fingerprint != partial.fingerprint


def test_weak_partial_target_is_rejected_immediately_with_channel():
    with pytest.raises(ValueError, match="通道 4 局部标定不充分"):
        CompensationProfile(baseline(), templates={3: baseline() + 20 + np.eye(16)[3] * MV_STEP})


def test_full_signed_probe_fit_recovers_point_bleed_without_global_drift():
    gains = np.linspace(0.8, 1.2, 16)
    matrix = np.eye(16) + np.roll(np.eye(16), 1, axis=0) * 0.25 - np.roll(np.eye(16), -1, axis=0) * 0.15
    profile = CompensationProfile(baseline(), gains, local_templates(matrix, gains))
    assert profile.correction_mode == "coupling"
    assert profile.fit_diagnostics["condition_number"] < 3
    np.testing.assert_allclose(profile.apply(baseline() + gains * 53), 0, atol=1e-12)
    for sign in (-1, 1):
        local = np.zeros(16)
        local[7] = sign * 10
        corrected = profile.apply(baseline() + matrix @ local - gains * 34)
        np.testing.assert_allclose(corrected, local, atol=0.005)


def test_collapsed_or_weak_sixteenth_template_cannot_replace_valid_partial():
    templates = local_templates(np.eye(16))
    partial = CompensationProfile(baseline(), templates={key: value for key, value in templates.items() if key != 15})
    old_fingerprint = partial.fingerprint
    templates[15] = baseline() + 5
    with pytest.raises(ValueError, match="局部标定不充分"):
        CompensationProfile(baseline(), templates=templates)
    assert partial.fingerprint == old_fingerprint
    assert partial.template_count == 15
    # Every column excites the same narrow region: 16 labelled probes add no rank.
    collapsed = {index: baseline() + np.r_[np.ones(8), np.zeros(8)] * (index + 3) for index in range(16)}
    with pytest.raises(ValueError):
        CompensationProfile(baseline(), templates=collapsed)
    nearly_singular = np.eye(16)
    nearly_singular[:, 1] = nearly_singular[:, 0]
    nearly_singular[1, 1] = 1e-3
    with pytest.raises(ValueError):
        CompensationProfile(baseline(), templates=local_templates(nearly_singular))
    amplifying = np.eye(16)
    amplifying[0, 1] = amplifying[1, 0] = 0.9
    with pytest.raises(ValueError, match="放大噪声"):
        CompensationProfile(baseline(), templates=local_templates(amplifying))


@pytest.mark.parametrize("bad", [None, [], [0] * 15, [0] * 17, [[0] * 16], [True] * 16, ["2"] * 16, [np.inf] * 16, [np.nan] * 16])
def test_invalid_frame_is_all_unknown(bad):
    assert np.isnan(CompensationProfile(baseline()).apply(bad)).all()


def test_roundtrip_stable_fingerprint_and_nonmutating_inputs():
    initial = baseline()
    templates = local_templates(np.eye(16))
    profile = CompensationProfile(initial, templates=templates)
    fingerprint = profile.fingerprint
    payload = json.loads(json.dumps(profile.to_dict(), allow_nan=False))
    restored = CompensationProfile.from_dict(payload)
    assert restored.fingerprint == fingerprint
    assert restored.to_dict() == payload
    frame = baseline() + 7 + np.eye(16)[3] * 4
    np.testing.assert_array_equal(restored.apply(frame), profile.apply(frame))
    initial[:] = -1
    templates[0][:] = -1
    # Even explicitly making a returned copy writeable cannot alter the model.
    for exposed in (profile.baseline, profile.common_gains, profile.templates[0]):
        exposed.setflags(write=True)
        exposed[:] = -10
    assert profile.fingerprint == fingerprint
    np.testing.assert_array_equal(profile.baseline, baseline())
    with pytest.raises(TypeError):
        profile.templates[1] = baseline()
    mutated = profile.to_dict()
    mutated["baseline_mv"][0] += 1
    assert CompensationProfile.from_dict(mutated).fingerprint != fingerprint


def test_fitted_gain_fingerprint_survives_floating_point_renormalization():
    rng = np.random.default_rng(913)
    for _ in range(100):
        profile = CompensationProfile(baseline(), rng.uniform(0.5, 2, 16))
        restored = CompensationProfile.from_dict(profile.to_dict())
        assert restored.fingerprint == profile.fingerprint
        np.testing.assert_array_equal(restored.common_gains, profile.common_gains)


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(version=True),
    lambda value: value.update(version=2),
    lambda value: value.update(kind="inspection-reference"),
    lambda value: value.update(algorithm="other"),
    lambda value: value.update(extra=1),
    lambda value: value.update(common_gains=[0] * 16),
    lambda value: value.update(baseline_mv=[None] * 16),
    lambda value: value.update(templates={"01": [0] * 16}),
    lambda value: value.update(templates={"16": [0] * 16}),
])
def test_strict_invalid_serialization(mutate):
    payload = copy.deepcopy(CompensationProfile(baseline()).to_dict())
    mutate(payload)
    with pytest.raises(ValueError):
        CompensationProfile.from_dict(payload)
