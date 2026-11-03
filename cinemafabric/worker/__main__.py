"""Run one model worker.

    python -m cinemafabric.worker --device cuda:0 --caps music,segment
    CF_JOIN_TOKEN=<token> python -m cinemafabric.worker --device mps --caps tts --join http://<fabric>:8100
"""

import argparse
import os
import socket
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="CinemaFabric worker")
    parser.add_argument("--device", default="auto", help="auto, cuda:<index>, mps or cpu")
    parser.add_argument("--gpu", default=None, help="shorthand for --device cuda:<index>")
    parser.add_argument("--port", type=int, default=8201)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--caps", default="tts,music,segment,watermark,depth",
                        help="comma list of: tts, music, segment, watermark, depth")
    parser.add_argument("--join", default="", help="fabric URL to register with")
    parser.add_argument("--token", default=os.environ.get("CF_JOIN_TOKEN", ""), help="prefer the CF_JOIN_TOKEN variable")
    parser.add_argument("--advertise", default="", help="URL the fabric should use to reach this worker")
    parser.add_argument("--name", default="", help="label shown in the UI")
    args = parser.parse_args()
    if any(a == "--token" or a.startswith("--token=") for a in sys.argv[1:]):
        print("note: --token is visible to other users in the process list; prefer CF_JOIN_TOKEN")

    device = f"cuda:{args.gpu}" if args.gpu is not None else args.device
    if device.startswith("cuda:"):
        os.environ["CUDA_VISIBLE_DEVICES"] = device.split(":", 1)[1]
        os.environ["CF_WORKER_DEVICE"] = "cuda"
    else:
        os.environ["CF_WORKER_DEVICE"] = device
    os.environ["CF_WORKER_CAPS"] = args.caps
    if args.join:
        host = args.host if args.host not in ("0.0.0.0", "::") else socket.gethostname()
        os.environ["CF_WORKER_JOIN"] = args.join
        os.environ["CF_WORKER_TOKEN"] = args.token
        os.environ["CF_WORKER_ADVERTISE"] = args.advertise or f"http://{host}:{args.port}"
        os.environ["CF_WORKER_ID"] = f"{socket.gethostname()}-{device.replace(':', '')}"
        os.environ["CF_WORKER_NAME"] = args.name
    uvicorn.run("cinemafabric.worker.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
