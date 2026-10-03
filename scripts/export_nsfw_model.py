"""Export Marqo's NSFW image classifier to ONNX for the NFT portfolio.

The portfolio runs the model with ONNX Runtime only. This one-off export needs
PyTorch and timm, which are not project dependencies:

    pip install torch timm onnx onnxscript onnxruntime
    python scripts/export_nsfw_model.py data/nsfw-image-detection-384.onnx

Then point NFT_PORTFOLIO_NSFW_MODEL at the written file.
Model: https://huggingface.co/Marqo/nsfw-image-detection-384 (Apache-2.0).
"""

import sys

import numpy as np
import onnxruntime
import timm
import torch

REVISION = "0c26ec22111b83f106d72a55f611ec35962bcb65"


def main(path: str) -> None:
    model = timm.create_model(
        f"hf_hub:Marqo/nsfw-image-detection-384@{REVISION}", pretrained=True
    ).eval()
    sample = torch.rand(2, 3, 384, 384) * 2 - 1
    torch.onnx.export(
        model,
        (sample,),
        path,
        input_names=["pixels"],
        output_names=["logits"],
        dynamic_axes={"pixels": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )
    with torch.no_grad():
        expected = model(sample).numpy()
    session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"])
    actual = session.run(None, {"pixels": sample.numpy()})[0]
    if not np.allclose(expected, actual, atol=1e-3):
        raise SystemExit("ONNX output differs from the PyTorch model.")
    print(f"wrote {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "nsfw-image-detection-384.onnx")
