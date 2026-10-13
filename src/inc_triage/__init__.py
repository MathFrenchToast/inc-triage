"""Moteur de qualification et de triage d'incidents techniques."""

from inc_triage.classify import classify, decide, main
from inc_triage.questions import QUESTIONS

__all__ = ["classify", "decide", "QUESTIONS", "main"]
