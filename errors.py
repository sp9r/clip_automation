class PipelineUserError(RuntimeError):
    def __init__(self, message, *, error_code, stage=None, retryable=False, user_action=None):
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.retryable = bool(retryable)
        self.user_action = user_action or "設定を確認してください。"

    def to_dict(self, traceback_text=None):
        data = {
            "type": type(self).__name__,
            "message": str(self),
            "error_code": self.error_code,
            "stage": self.stage,
            "retryable": self.retryable,
            "user_action": self.user_action,
        }
        if traceback_text:
            data["traceback"] = traceback_text
        return data
