class PipelineCancelled(RuntimeError):
    """ユーザー操作によってパイプラインが安全に中断されたことを表す。"""


def raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise PipelineCancelled("処理はユーザーによって停止されました。")
