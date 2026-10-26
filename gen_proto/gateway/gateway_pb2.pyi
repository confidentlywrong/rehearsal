from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Origin(_message.Message):
    __slots__ = ("type", "id", "session_id")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    type: str
    id: str
    session_id: str
    def __init__(self, type: _Optional[str] = ..., id: _Optional[str] = ..., session_id: _Optional[str] = ...) -> None: ...

class Wrapper(_message.Message):
    __slots__ = ("timestamp", "type", "request_id", "session_id", "payload", "origin", "destination", "status", "event", "metadata", "simulation_id")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    SIMULATION_ID_FIELD_NUMBER: _ClassVar[int]
    timestamp: str
    type: str
    request_id: str
    session_id: str
    payload: bytes
    origin: Origin
    destination: _containers.RepeatedScalarFieldContainer[str]
    status: str
    event: str
    metadata: bytes
    simulation_id: str
    def __init__(self, timestamp: _Optional[str] = ..., type: _Optional[str] = ..., request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., payload: _Optional[bytes] = ..., origin: _Optional[_Union[Origin, _Mapping]] = ..., destination: _Optional[_Iterable[str]] = ..., status: _Optional[str] = ..., event: _Optional[str] = ..., metadata: _Optional[bytes] = ..., simulation_id: _Optional[str] = ...) -> None: ...

class BroadcastRequest(_message.Message):
    __slots__ = ("payload", "target_session_ids")
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    TARGET_SESSION_IDS_FIELD_NUMBER: _ClassVar[int]
    ASYNC_FIELD_NUMBER: _ClassVar[int]
    payload: bytes
    target_session_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, payload: _Optional[bytes] = ..., target_session_ids: _Optional[_Iterable[str]] = ..., **kwargs) -> None: ...

class BatchResponse(_message.Message):
    __slots__ = ("request_id", "responses")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSES_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    responses: _containers.RepeatedCompositeFieldContainer[Wrapper]
    def __init__(self, request_id: _Optional[str] = ..., responses: _Optional[_Iterable[_Union[Wrapper, _Mapping]]] = ...) -> None: ...

class ToolCallRequest(_message.Message):
    __slots__ = ("resource_type", "params")
    class ParamsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    RESOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    resource_type: str
    params: _containers.ScalarMap[str, str]
    def __init__(self, resource_type: _Optional[str] = ..., params: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ToolCallResponse(_message.Message):
    __slots__ = ("data", "error")
    DATA_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    error: str
    def __init__(self, data: _Optional[bytes] = ..., error: _Optional[str] = ...) -> None: ...

class A2UIAction(_message.Message):
    __slots__ = ("session_id", "action_name", "context")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_NAME_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    action_name: str
    context: bytes
    def __init__(self, session_id: _Optional[str] = ..., action_name: _Optional[str] = ..., context: _Optional[bytes] = ...) -> None: ...

class ResourcePool(_message.Message):
    __slots__ = ("current", "initial", "regen_per_tick", "critical_threshold")
    CURRENT_FIELD_NUMBER: _ClassVar[int]
    INITIAL_FIELD_NUMBER: _ClassVar[int]
    REGEN_PER_TICK_FIELD_NUMBER: _ClassVar[int]
    CRITICAL_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    current: float
    initial: float
    regen_per_tick: float
    critical_threshold: float
    def __init__(self, current: _Optional[float] = ..., initial: _Optional[float] = ..., regen_per_tick: _Optional[float] = ..., critical_threshold: _Optional[float] = ...) -> None: ...

class OrganizationalState(_message.Message):
    __slots__ = ("resources", "active_alerts", "contradiction_log", "completion_percentage")
    class ResourcesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ResourcePool
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[ResourcePool, _Mapping]] = ...) -> None: ...
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_ALERTS_FIELD_NUMBER: _ClassVar[int]
    CONTRADICTION_LOG_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_PERCENTAGE_FIELD_NUMBER: _ClassVar[int]
    resources: _containers.MessageMap[str, ResourcePool]
    active_alerts: _containers.RepeatedScalarFieldContainer[str]
    contradiction_log: _containers.RepeatedScalarFieldContainer[str]
    completion_percentage: float
    def __init__(self, resources: _Optional[_Mapping[str, ResourcePool]] = ..., active_alerts: _Optional[_Iterable[str]] = ..., contradiction_log: _Optional[_Iterable[str]] = ..., completion_percentage: _Optional[float] = ...) -> None: ...

class SynchronicityAlert(_message.Message):
    __slots__ = ("alert_id", "severity", "alert_type", "description", "conflicting_initiatives", "recommended_guardrails", "affected_resources")
    ALERT_ID_FIELD_NUMBER: _ClassVar[int]
    SEVERITY_FIELD_NUMBER: _ClassVar[int]
    ALERT_TYPE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    CONFLICTING_INITIATIVES_FIELD_NUMBER: _ClassVar[int]
    RECOMMENDED_GUARDRAILS_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    alert_id: str
    severity: str
    alert_type: str
    description: str
    conflicting_initiatives: _containers.RepeatedScalarFieldContainer[str]
    recommended_guardrails: _containers.RepeatedScalarFieldContainer[str]
    affected_resources: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, alert_id: _Optional[str] = ..., severity: _Optional[str] = ..., alert_type: _Optional[str] = ..., description: _Optional[str] = ..., conflicting_initiatives: _Optional[_Iterable[str]] = ..., recommended_guardrails: _Optional[_Iterable[str]] = ..., affected_resources: _Optional[_Iterable[str]] = ...) -> None: ...
