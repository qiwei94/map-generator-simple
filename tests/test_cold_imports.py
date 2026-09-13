import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_layer_preprocess_cold_import_does_not_enter_aesthetic_loop_cycle():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess "
                "import PREPROCESS_POLICY_VERSION; "
                "print(PREPROCESS_POLICY_VERSION)"
            ),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "block_topology=scale-aware-block-topology-v3-valid-faces" in result.stdout


def test_lazy_aesthetic_public_api_remains_compatible():
    import aesthetic

    assert aesthetic.CityPreset.__name__ == "CityPreset"
    assert aesthetic.AestheticLoop.__name__ == "AestheticLoop"
