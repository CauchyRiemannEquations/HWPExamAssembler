"""Application services used by the desktop UI."""

from .exam_service import (
    DocumentSnapshot,
    ExamDocumentService,
    QuestionSummary,
)
from .live_import import LiveImportSession, RegisteredQuestion
from .auto_qid import (
    DetectedQuestion,
    ParagraphRecord,
    create_auto_qid_copy,
    detect_questions,
    detect_questions_from_records,
    scan_paragraphs,
)
from .external_edit import FileFingerprint, file_fingerprint, file_is_in_use
from .school_profile import (
    FinalCheckProfile,
    SchoolProfile,
    SubjectLayoutProfile,
    load_school_profile,
)
from .structure_analysis import (
    ImportUnit,
    StructureAnalysis,
    StructureItem,
    analyze_structure,
    plan_import_units,
    validate_import_units,
)

__all__ = [
    "DocumentSnapshot",
    "ExamDocumentService",
    "LiveImportSession",
    "QuestionSummary",
    "RegisteredQuestion",
    "DetectedQuestion",
    "ParagraphRecord",
    "create_auto_qid_copy",
    "detect_questions",
    "detect_questions_from_records",
    "scan_paragraphs",
    "FileFingerprint",
    "file_fingerprint",
    "file_is_in_use",
    "SchoolProfile",
    "SubjectLayoutProfile",
    "FinalCheckProfile",
    "load_school_profile",
    "StructureAnalysis",
    "StructureItem",
    "analyze_structure",
    "ImportUnit",
    "plan_import_units",
    "validate_import_units",
]
