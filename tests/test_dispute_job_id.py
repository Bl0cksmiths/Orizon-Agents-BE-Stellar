"""The derived id a dispute's rating is written under (story 4.04, R12).

Pinned hard, because it is permanent in a way ordinary code is not: the
ReputationLedger's replay guard remembers every `(agent_id, job_id)` it has
seen, forever. Change this derivation after one rating has landed and a retry
of that dispute derives a NEW id, the guard no longer recognises it, and the
agent is rated twice for one dispute.
"""

from __future__ import annotations

import pytest

from app.services.dispute_rating import DISPUTE_ID_TAG, JOB_ID_BYTES, dispute_job_id

_JOB = bytes(range(16))


def test_the_derivation_matches_its_golden_vectors() -> None:
    # Recomputed independently of the function (sha256 over job ‖ tag ‖ step,
    # first eight bytes, behind the job's own first eight). A failure here is
    # not a test to update: it means already-written ratings no longer match.
    assert dispute_job_id(_JOB, 0).hex() == "00010203040506071e6388cbecdde018"
    assert dispute_job_id(_JOB, 3).hex() == "000102030405060791ded15f2cf43f7e"
    assert DISPUTE_ID_TAG == b"orizon-dispute:v1"


def test_the_derived_id_is_the_ledgers_width_and_deterministic() -> None:
    derived = dispute_job_id(_JOB, 2)
    assert len(derived) == JOB_ID_BYTES
    assert derived == dispute_job_id(_JOB, 2)


def test_the_first_half_is_the_sealed_job_so_a_reviewer_can_see_the_link() -> None:
    # SOW §6.1: whoever opens the rating on Stellar Expert must be able to tie
    # it to the attested job without reading this code.
    assert dispute_job_id(_JOB, 5)[:8] == _JOB[:8]


def test_each_step_of_one_job_gets_its_own_id() -> None:
    # The defect 4.04 fixes: one agent serving two steps of a job used to
    # derive ONE id for both, so the second upheld dispute was refused as a
    # replay of the first.
    ids = {dispute_job_id(_JOB, step) for step in range(64)}
    assert len(ids) == 64


def test_the_derived_id_never_lands_on_the_jobs_own_key() -> None:
    # The settler's auto-rating already holds Rated(agent_id, job_id).
    for step in range(64):
        assert dispute_job_id(_JOB, step) != _JOB


def test_two_jobs_sharing_a_prefix_still_derive_different_ids() -> None:
    other = _JOB[:8] + bytes(8)
    assert dispute_job_id(_JOB, 0) != dispute_job_id(other, 0)


@pytest.mark.parametrize("job_id", [b"", bytes(15), bytes(17), bytes(32)])
def test_a_job_id_of_the_wrong_width_is_refused(job_id: bytes) -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        dispute_job_id(job_id, 0)


@pytest.mark.parametrize("step_index", [-1, 65_536])
def test_a_step_index_that_does_not_fit_is_refused(step_index: int) -> None:
    with pytest.raises(ValueError, match="does not fit"):
        dispute_job_id(_JOB, step_index)


def test_a_derived_id_equal_to_the_job_id_is_refused_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unreachable by chance (2**-64), so forced: a digest whose first eight
    # bytes reproduce the job id's own tail.
    import hashlib

    class _Echo:
        def __init__(self, data: bytes) -> None:
            self._tail = data[8:16]

        def digest(self) -> bytes:
            return self._tail + bytes(24)

    monkeypatch.setattr(hashlib, "sha256", _Echo)
    with pytest.raises(ValueError, match="equals the job id itself"):
        dispute_job_id(_JOB, 0)
