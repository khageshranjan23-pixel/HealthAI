"""
Dr.SushamHealthAI — models/lesion_models.py
SQLAlchemy ORM models for longitudinal lesion tracking.

Two tables:
  - LesionProfile: represents a specific lesion on a patient's body
  - LesionSnapshot: a single observation of that lesion at a point in time

This enables tracking the same lesion over weeks/months and computing
growth rates, colour changes, and malignancy trajectory.
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, Integer, Float, DateTime, Text, ForeignKey, JSON, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class LesionProfile(Base):
    """A specific lesion on a patient that is being tracked over time."""

    __tablename__ = "lesion_profiles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(64), nullable=False, index=True)
    body_location = Column(String(128), nullable=True)  # e.g., "left forearm", "upper back"
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    # Relationship to snapshots
    snapshots = relationship("LesionSnapshot", back_populates="lesion",
                             order_by="LesionSnapshot.timestamp")


class LesionSnapshot(Base):
    """A single observation (photo + analysis) of a tracked lesion."""

    __tablename__ = "lesion_snapshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    lesion_id = Column(String(36), ForeignKey("lesion_profiles.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    # Image storage
    image_path = Column(String(512), nullable=True)  # filesystem path to saved image

    # ABCDE+ scores at this point in time
    asymmetry_score = Column(Integer, nullable=True)
    border_score = Column(Integer, nullable=True)
    color_score = Column(Integer, nullable=True)
    diameter_score = Column(Integer, nullable=True)
    diameter_mm = Column(Float, nullable=True)
    overall_risk = Column(Integer, nullable=True)
    uncertainty = Column(Integer, nullable=True)

    # Skin tone at time of capture
    fitzpatrick_type = Column(String(4), nullable=True)
    fitzpatrick_ita = Column(Float, nullable=True)

    # Disease prediction
    predicted_disease = Column(String(256), nullable=True)
    prediction_confidence = Column(Float, nullable=True)

    # Full analysis data (JSON blob for flexibility)
    analysis_json = Column(JSON, nullable=True)

    # Relationship
    lesion = relationship("LesionProfile", back_populates="snapshots")
