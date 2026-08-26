"""Vehicle ID: the vision stage of the lane.

Imported lazily by the lane controller. The core package stays dependency-free
-- `pip install -e '.[dev]'` needs no torch, no OpenCV -- so the simulated lane
and its tests keep running on any machine. Vision is `.[vision]`.
"""
