from __future__ import annotations

import json

import torch


def main() -> int:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable")
    left = torch.ones((2, 2), device="cuda")
    right = torch.ones((2, 2), device="cuda")
    result = torch.mm(left, right)
    torch.cuda.synchronize()
    if tuple(result.shape) != (2, 2):
        raise RuntimeError("CUDA mm returned an invalid shape")
    print(
        json.dumps(
            {
                "status": "pass",
                "cuda": True,
                "device": torch.cuda.get_device_name(torch.cuda.current_device()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
