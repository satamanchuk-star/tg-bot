"""Почему: автоматическая калибровка модерации по голосам участников лог-чата."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModerationCalibration, ModerationTraining

logger = logging.getLogger(__name__)


async def recalibrate_moderation(session: AsyncSession) -> int:
    """Анализирует голоса за последние 7 дней и корректирует severity-маппинг.

    Возвращает количество записанных корректировок.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    samples_result = await session.execute(
        select(ModerationTraining).where(
            and_(
                ModerationTraining.created_at >= cutoff,
                (ModerationTraining.vote_yes + ModerationTraining.vote_no) >= 3,
            )
        )
    )
    samples = samples_result.scalars().all()

    adjustments = 0
    for sample in samples:
        total_votes = sample.vote_yes + sample.vote_no
        if total_votes < 3:
            continue

        false_positive_ratio = sample.vote_no / total_votes
        if false_positive_ratio >= 0.7 and sample.ai_severity >= 2:
            # Большинство считает, что это ложное срабатывание
            adjusted = max(0, sample.ai_severity - 1)
            session.add(ModerationCalibration(
                violation_type=sample.ai_violation_type or "unknown",
                original_severity=sample.ai_severity,
                adjusted_severity=adjusted,
                reason=f"False positive ratio {false_positive_ratio:.0%} ({total_votes} votes)",
                sample_count=total_votes,
            ))
            adjustments += 1
            logger.info(
                "CALIBRATION: violation_type=%s severity %d->%d ratio=%.0f%% samples=%d",
                sample.ai_violation_type, sample.ai_severity, adjusted,
                false_positive_ratio * 100, total_votes,
            )

    if adjustments:
        await session.commit()
        logger.info("CALIBRATION: recorded %d adjustments", adjustments)

    return adjustments
