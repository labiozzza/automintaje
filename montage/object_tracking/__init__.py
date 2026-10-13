"""Object Tracking package (optional, off by default).

Isolated from the main app process: nothing here imports torch/sam2 at
package import time.  Heavy work runs in ``sam_worker.py`` under the isolated
``~/.venvs/sam2`` interpreter as a subprocess.  When Object Tracking is
disabled, none of this code touches the render pipeline.
"""
