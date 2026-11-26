# Vendored OpenTelemetry Protobuf Python Modules

These modules are generated from the upstream `opentelemetry-proto` repository
and vendored here under the Apache License, Version 2.0 (see `LICENSE` in this
directory).

- Upstream repository: https://github.com/open-telemetry/opentelemetry-proto
- Pinned release tag: `v1.7.0` (matching `opentelemetry-proto==1.37.0`)
- Runtime compatibility: `protobuf==5.29.3` (generated with Protobuf Python 5.26.1)

## Included proto files

- `opentelemetry/proto/common/v1/common.proto` -> `common_pb2.py`
- `opentelemetry/proto/resource/v1/resource.proto` -> `resource_pb2.py`
- `opentelemetry/proto/trace/v1/trace.proto` -> `trace_pb2.py`
- `opentelemetry/proto/collector/trace/v1/trace_service.proto` -> `trace_service_pb2.py`

## Exact generation command

From a checkout of `opentelemetry-proto` at tag `v1.7.0`:

```bash
python -m grpc_tools.protoc \
  -I . \
  --python_out=src/arch_indexer/runtime/_otlp \
  opentelemetry/proto/common/v1/common.proto \
  opentelemetry/proto/resource/v1/resource.proto \
  opentelemetry/proto/trace/v1/trace.proto \
  opentelemetry/proto/collector/trace/v1/trace_service.proto
```

After generation, internal `from opentelemetry.proto.` imports in the generated
`*_pb2.py` files are scoped to `arch_indexer.runtime._otlp.opentelemetry.proto.`
and `# ruff: noqa` is prepended so the vendored package is completely
self-contained and passes repository lint checks without requiring
`opentelemetry-proto` at runtime.
