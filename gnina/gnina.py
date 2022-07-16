import argparse
import os
from collections import OrderedDict
from typing import List, Optional, Union

import torch

from gnina import dataloaders, models, setup, utils


def _rename(key: str) -> str:
    """
    Rename GNINA layer to PyTorch layer.

    Parameters
    ----------
    key: str
        GNINA layer name (in loaded state dict)

    Returns
    -------
    str
        PyTorch layer name

    Raises
    ------
    RuntimeError
        if layer name is unknown

    Notes
    -----
    The PyTorch CNN laters are named similarly to the original Caffe layers. However,
    the layer name is prepended with "features.". PyTorch fully connected layers are
    called differently.

    The Default2017 model has slighlt different naming convention than Default2018 and
    dense models.
    """
    # Fix CNN layer names for all models
    if "conv" in key:
        return f"features.{key}"
    # Fix default2017 model names
    elif "output_fc." in key:
        return key.replace("output_fc", "pose.pose_output")
    elif "output_fc_aff." in key:
        return key.replace("output_fc_aff", "affinity.affinity_output")
    # Fix default2018 and dense models
    elif "pose_output" in key:
        return f"pose.{key}"
    elif "affinity_output" in key:
        return f"affinity.{key}"
    else:  # This should never happen
        raise RuntimeError(f"Unknown layer name: {key}")


def _load_weights(weights_file: str):
    """
    Load weights from file.

    Parameters
    ----------
    weights_file: str
        Path to weights file

    Returns
    -------
    OrderedDict
        Dictionary of weights (renamed according to PyTorch layer names)
    """
    weights = torch.load(weights_file)

    # Rename Caffe layers according to PyTorch names defined in gnina.models
    weights_renamed = OrderedDict(
        ((_rename(key), value) for key, value in weights.items())
    )

    return weights_renamed


def _load_gnina_model_file(
    weights_file: str, num_voxels: int
) -> Union[models.Default2017Affinity, models.Default2018Affinity]:
    """
    Load GNINA model from file.

    Parameters
    ----------
    weights_file: str
        Path to weights file
    num_voxels: int
        Number of voxels per grid dimension

    Note
    ----
    All GNINA default models perform both pose prediction and binding affinity
    prediction.
    """
    if "default2017" in weights_file:
        # 32 channels: 18 for the ligand (ligmap.old) and 14 for the protein
        model: Union[
            models.Default2017Affinity, models.Default2018Affinity
        ] = models.Default2017Affinity(
            input_dims=(35, num_voxels, num_voxels, num_voxels)
        )
    elif "default2018" in weights_file:
        # 28 channels:
        #   14 for the ligand (completelig) and 14 for the protein (completerec)
        model = models.Default2018Affinity(
            input_dims=(28, num_voxels, num_voxels, num_voxels)
        )
    elif "dense" in weights_file:
        # TODO: Load dense model (name conversions are a bit of a pain...)
        raise NotImplementedError("Dense models are not supported yet.")
    else:
        raise ValueError(f"Unknown model name: {weights_file}")

    weights = _load_weights(weights_file)

    model.load_state_dict(weights)

    return model


def load_gnina_model(
    gnina_model: str, dimension: float = 23.5, resolution: float = 0.5
):
    """
    Load GNINA model.

    Parameters
    ----------
    gnina_model: str
        GNINA model name
    """
    path = os.path.dirname(os.path.abspath(__file__))
    gnina_model_file = os.path.join(path, "weights", f"{gnina_model}.pt")

    # Fromhttps://github.com/gnina/libmolgrid/include/libmolgrid/grid_maker.h
    num_voxels = round(dimension / resolution) + 1

    return _load_gnina_model_file(gnina_model_file, num_voxels)


def options(args: Optional[List[str]] = None):
    """
    Define options and parse arguments.

    Parameters
    ----------
    args: Optional[List[str]]
        List of command line arguments
    """
    parser = argparse.ArgumentParser(
        description=" GNINA scoring function",
    )

    parser.add_argument("input", type=str, help="Input file for inference")

    # TODO: Allow only known keywords
    # TODO: Allow multiple models as model ensemble
    # TODO: Default2017 model needs different ligand types
    parser.add_argument(
        "--cnn",
        type=str,
        help="Pre-trained CNN Model",
        default="crossdock_default2018",  # TODO: change to default model ensemble
        choices=["crossdock_default2018"]
        + [f"crossdock_default2018_{i}" for i in range(1, 5)]
        + ["general_default2018"]
        + [f"general_default2018_{i}" for i in range(1, 5)]
        + ["redock_default2018"]
        + [f"redock_default2018_{i}" for i in range(1, 5)],
    )

    parser.add_argument(
        "-d",
        "--data_root",
        type=str,
        default="",
        help="Root folder for relative paths in train files",
    )

    parser.add_argument("-g", "--gpu", type=str, default="cuda:0", help="Device name")

    parser.add_argument("--dimension", type=float, default=23.5, help="Grid dimension")
    parser.add_argument("--resolution", type=float, default=0.5, help="Grid resolution")
    parser.add_argument("--batch_size", type=int, default=64, help="Batch size")

    parser.add_argument(
        "--ligmolcache",
        type=str,
        default="",
        help=".molcache2 file for ligands",
    )
    parser.add_argument(
        "--recmolcache",
        type=str,
        default="",
        help=".molcache2 file for receptors",
    )

    return parser.parse_args(args)


def gnina(args):
    model = load_gnina_model(args.cnn, args.dimension, args.resolution)

    device = utils.set_device(args.gpu)
    model.to(device)

    example_provider = setup.setup_example_provider(args.input, args, training=False)
    grid_maker = setup.setup_grid_maker(args)

    # TODO: Allow average over different rotations
    loader = dataloaders.GriddedExamplesLoader(
        example_provider=example_provider,
        grid_maker=grid_maker,
        random_translation=0.0,  # No random translations for inference
        random_rotation=False,  # No random rotations for inference
        device=device,
        grids_only=True,
    )

    for batch in loader:
        log_pose, affinity = model(batch)

        pose = torch.exp(log_pose[:, -1])

        for p, a in zip(pose, affinity):
            print(f"CNNscore: {p:.5f}")
            print(f"CNNaffinity: {a:.5f}", "\n")


def _intro():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    logo_file = os.path.join(path, "logo")
    with open(logo_file, "r") as f:
        logo = f.read()

    into_file = os.path.join(path, "intro")
    with open(into_file, "r") as f:
        intro = f.read()

    print(logo, "\n\n", intro, "\n")


if __name__ == "__main__":
    _intro()
    args = options()
    gnina(args)
