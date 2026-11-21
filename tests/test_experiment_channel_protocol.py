"""Wire format must not execute code received from an authenticated peer."""
import pickle
from multiprocessing import Pipe

import pytest

from devflow_ai.experiment_channel import (
    BudgetChannelError, MAX_MESSAGE_BYTES, _receive_json, _send_json, connection_transport,
)


def test_pickle_payload_is_rejected_without_running_its_reduce(tmp_path):
    marker = tmp_path / "executed"

    class Payload:
        def __reduce__(self):
            return (eval, (f"__import__('pathlib').Path({str(marker)!r}).write_text('bad')",))

    receiver, sender = Pipe()
    try:
        sender.send_bytes(pickle.dumps(Payload()))
        with pytest.raises(BudgetChannelError):
            _receive_json(receiver)
        assert not marker.exists()
    finally:
        receiver.close()
        sender.close()


def test_json_wire_format_rejects_oversized_message():
    receiver, sender = Pipe()
    try:
        with pytest.raises(BudgetChannelError, match="too large"):
            _send_json(sender, {"payload": "x" * MAX_MESSAGE_BYTES})
    finally:
        receiver.close()
        sender.close()


def test_response_timeout_closes_stream_to_prevent_late_reply_confusion():
    client, server = Pipe()
    try:
        transport = connection_transport(client, timeout_seconds=0.01)
        with pytest.raises(BudgetChannelError, match="time"):
            transport({"op": "reserve"})
        assert client.closed
    finally:
        client.close()
        server.close()
