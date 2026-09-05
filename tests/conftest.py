import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tests do not need the real docker SDK installed.
_fake_docker = types.ModuleType("docker")
_fake_docker_errors = types.ModuleType("docker.errors")
_fake_docker_errors.DockerException = Exception
_fake_docker_errors.NotFound = Exception
_fake_docker.errors = _fake_docker_errors
sys.modules.setdefault("docker", _fake_docker)
sys.modules.setdefault("docker.errors", _fake_docker_errors)
