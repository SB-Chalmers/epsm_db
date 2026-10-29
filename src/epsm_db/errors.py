"""Actionable domain retrieval errors without connection details."""


class PublicationPendingError(RuntimeError):
    """The linked, GIS and model publication revisions do not agree."""


class RetrievalError(LookupError):
    pass


class BoundaryUnavailableError(RetrievalError):
    pass


class RecordNotFoundError(RetrievalError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__("No matching building in the selected stock")


class AmbiguousRecordError(RetrievalError):
    def __init__(self, candidates):
        self.candidates = candidates
        self.receipt = candidates.receipt
        super().__init__("Multiple matching buildings; inspect candidates")
