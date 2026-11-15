# Public evaluation image for Click #3084. No cases, no credentials, no project files.
#
# The evaluation harness mounts the candidate checkout read-only at /candidate and runs
# the material's `command` as the container entrypoint, so the adapter has to live inside
# the image: nothing else is mounted. Build it with the pack directory as context:
#
#   docker build -t click3084-eval:local benchmarks/click_3084_public
#
# The hidden stdin/stdout cases are supplied by the trusted control plane at run time; they
# are never part of this image.
FROM python:3.11-slim

COPY stdio_adapter.py /opt/devflow/click3084_adapter.py

WORKDIR /candidate
ENTRYPOINT ["python", "-B", "/opt/devflow/click3084_adapter.py"]
