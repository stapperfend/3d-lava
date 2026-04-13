"""
drivers/cameras.py  —  Camera stream stubs
==========================================
Camera integration is deferred. This module provides placeholder
endpoints. Each camera type will be implemented here when ready:

  - Basler:   pypylon SDK → MJPEG stream via Flask generator
  - Optris:   PIX Connect SDK / HTTP/RTSP stream proxy

To add a camera:
  1. Add an entry to config.CAMERAS
  2. Implement the corresponding _stream_<type>() generator below
  3. The Flask route /stream/camera/<id> will auto-pick it up
"""

import config


def list_cameras() -> list[dict]:
    """Return metadata for all configured cameras."""
    return [{"id": k, "type": v.get("type", "unknown")} for k, v in config.CAMERAS.items()]


def get_stream_generator(camera_id: str):
    """
    Return an MJPEG generator for the given camera ID, or None if unavailable.
    Each frame must be yielded as:  b'--frame\\r\\nContent-Type: image/jpeg\\r\\n\\r\\n' + jpeg_bytes + b'\\r\\n'
    """
    cam = config.CAMERAS.get(camera_id)
    if cam is None:
        return None

    cam_type = cam.get("type")

    if cam_type == "basler":
        return _stream_basler(cam)
    elif cam_type == "optris":
        return _stream_optris(cam)
    else:
        return None


# ---------------------------------------------------------------------------
# Camera type implementations  (TO BE IMPLEMENTED)
# ---------------------------------------------------------------------------

def _stream_basler(cam_config: dict):
    """
    TODO: Implement Basler camera streaming.
    Requires:  pip install pypylon
    Example:
        from pypylon import pylon
        camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
        camera.Open()
        camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        while camera.IsGrabbing():
            grab = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
            if grab.GrabSucceeded():
                img = grab.Array   # numpy array
                # encode to JPEG and yield frame
        camera.StopGrabbing()
    """
    raise NotImplementedError("Basler camera support not yet implemented.")


def _stream_optris(cam_config: dict):
    """
    TODO: Implement Optris thermal camera streaming.
    Options:
      A) Use the HTTP/RTSP stream URL from cam_config["url"] and proxy it.
      B) Use the Optris PIX Connect SDK (IRImager / libirimager).
    """
    raise NotImplementedError("Optris camera support not yet implemented.")
