import collections
import os
from copy import deepcopy
from itertools import product
from pathlib import Path
from typing import Dict, List, Optional, OrderedDict, Sequence, Tuple, Union

import imageio
import numpy as np
import xarray as xr
from tqdm import tqdm

from bioimageio.core import load_resource_description
from bioimageio.core.prediction_pipeline import PredictionPipeline, create_prediction_pipeline
from bioimageio.core.resource_io.nodes import ImplicitOutputShape, InputTensor, Model, ResourceDescription, OutputTensor
from bioimageio.spec.shared import raw_nodes
from bioimageio.spec.shared.raw_nodes import ResourceDescription as RawResourceDescription


#
# utility functions for prediction
#
def _require_axes(im, axes):
    is_volume = "z" in axes
    # we assume images / volumes are loaded as one of
    # yx, yxc, zyxc
    if im.ndim == 2:
        im_axes = ("y", "x")
    elif im.ndim == 3:
        im_axes = ("z", "y", "x") if is_volume else ("y", "x", "c")
    elif im.ndim == 4:
        raise NotImplementedError
    else:  # ndim >= 5 not implemented
        raise RuntimeError

    # add singleton channel dimension if not present
    if "c" not in im_axes:
        im = im[..., None]
        im_axes = im_axes + ("c",)

    # add singleton batch dim
    im = im[None]
    im_axes = ("b",) + im_axes

    # permute the axes correctly
    assert set(axes) == set(im_axes)
    axes_permutation = tuple(im_axes.index(ax) for ax in axes)
    im = im.transpose(axes_permutation)
    return im


def _pad(im, axes: Sequence[str], padding, pad_right=True) -> Tuple[np.ndarray, Dict[str, slice]]:
    assert im.ndim == len(axes), f"{im.ndim}, {len(axes)}"

    padding_ = deepcopy(padding)
    mode = padding_.pop("mode", "dynamic")
    assert mode in ("dynamic", "fixed")

    is_volume = "z" in axes
    if is_volume:
        assert len(padding_) == 3
    else:
        assert len(padding_) == 2

    if isinstance(pad_right, bool):
        pad_right = len(axes) * [pad_right]

    pad_width = []
    crop = {}
    for ax, dlen, pr in zip(axes, im.shape, pad_right):

        if ax in "zyx":
            pad_to = padding_[ax]

            if mode == "dynamic":
                r = dlen % pad_to
                pwidth = 0 if r == 0 else (pad_to - r)
            else:
                if pad_to < dlen:
                    msg = f"Padding for axis {ax} failed; pad shape {pad_to} is smaller than the image shape {dlen}."
                    raise RuntimeError(msg)
                pwidth = pad_to - dlen

            pad_width.append([0, pwidth] if pr else [pwidth, 0])
            crop[ax] = slice(0, dlen) if pr else slice(pwidth, None)
        else:
            pad_width.append([0, 0])
            crop[ax] = slice(None)

    im = np.pad(im, pad_width, mode="symmetric")
    return im, crop


def _load_image(in_path, axes: Sequence[str]) -> xr.DataArray:
    ext = os.path.splitext(in_path)[1]
    if ext == ".npy":
        im = np.load(in_path)
    else:
        is_volume = "z" in axes
        im = imageio.volread(in_path) if is_volume else imageio.imread(in_path)
        im = _require_axes(im, axes)
    return xr.DataArray(im, dims=axes)


def _load_tensors(sources, tensor_specs: List[Union[InputTensor, OutputTensor]]) -> List[xr.DataArray]:
    return [_load_image(s, sspec.axes) for s, sspec in zip(sources, tensor_specs)]


def _to_channel_last(image):
    chan_id = image.dims.index("c")
    if chan_id != image.ndim - 1:
        target_axes = tuple(ax for ax in image.dims if ax != "c") + ("c",)
        image = image.transpose(*target_axes)
    return image


def _save_image(out_path, image):
    ext = os.path.splitext(out_path)[1]
    if ext == ".npy":
        np.save(out_path, image)
    else:
        is_volume = "z" in image.dims

        # squeeze batch or channel axes if they are singletons
        squeeze = {ax: 0 if (ax in "bc" and sh == 1) else slice(None) for ax, sh in zip(image.dims, image.shape)}
        image = image[squeeze]

        if "b" in image.dims:
            raise RuntimeError(f"Cannot save prediction with batchsize > 1 as {ext}-file")
        if "c" in image.dims:  # image formats need channel last
            image = _to_channel_last(image)

        save_function = imageio.volsave if is_volume else imageio.imsave
        # most image formats only support channel dimensions of 1, 3 or 4;
        # if not we need to save the channels separately
        ndim = 3 if is_volume else 2
        save_as_single_image = image.ndim == ndim or (image.shape[-1] in (3, 4))

        if save_as_single_image:
            save_function(out_path, image)
        else:
            out_prefix, ext = os.path.splitext(out_path)
            for c in range(image.shape[-1]):
                chan_out_path = f"{out_prefix}-c{c}{ext}"
                save_function(chan_out_path, image[..., c])


def _apply_crop(data, crop):
    crop = tuple(crop[ax] for ax in data.dims)
    return data[crop]


def _get_tiling(shape, tile_shape, halo, input_axes):
    assert len(shape) == len(input_axes)

    shape_ = [sh for sh, ax in zip(shape, input_axes) if ax in "xyz"]
    spatial_axes = [ax for ax in input_axes if ax in "xyz"]
    tile_shape_ = [tile_shape[ax] for ax in spatial_axes]
    halo_ = [halo[ax] for ax in spatial_axes]
    assert len(shape_) == len(tile_shape_) == len(spatial_axes) == len(halo_)

    ranges = [range(sh // tsh if sh % tsh == 0 else sh // tsh + 1) for sh, tsh in zip(shape_, tile_shape_)]
    start_points = product(*ranges)

    for start_point in start_points:
        positions = [sp * tsh for sp, tsh in zip(start_point, tile_shape_)]

        outer_tile = {
            ax: slice(max(pos - ha, 0), min(pos + tsh + ha, sh))
            for ax, pos, tsh, sh, ha in zip(spatial_axes, positions, tile_shape_, shape_, halo_)
        }
        outer_tile["b"] = slice(None)
        outer_tile["c"] = slice(None)

        inner_tile = {
            ax: slice(pos, min(pos + tsh, sh)) for ax, pos, tsh, sh in zip(spatial_axes, positions, tile_shape_, shape_)
        }
        inner_tile["b"] = slice(None)
        inner_tile["c"] = slice(None)

        local_tile = {
            ax: slice(
                inner_tile[ax].start - outer_tile[ax].start,
                -(outer_tile[ax].stop - inner_tile[ax].stop) if outer_tile[ax].stop != inner_tile[ax].stop else None,
            )
            for ax in spatial_axes
        }
        local_tile["b"] = slice(None)
        local_tile["c"] = slice(None)

        yield outer_tile, inner_tile, local_tile


def _predict_with_tiling_impl(
    prediction_pipeline: PredictionPipeline,
    # TODO this can be anything with a numpy-like interface
    inputs: List[xr.DataArray],
    outputs: List[xr.DataArray],
    tile_shapes: List[dict],
    halos: List[dict],
    verbose: bool = False,
):
    if len(inputs) > 1:
        raise NotImplementedError("Tiling with multiple inputs not implemented yet")

    if len(outputs) > 1:
        raise NotImplementedError("Tiling with multiple outputs not implemented yet")

    assert len(tile_shapes) == len(outputs)
    assert len(halos) == len(outputs)

    input_ = inputs[0]
    output = outputs[0]
    tile_shape = tile_shapes[0]
    halo = halos[0]

    tiles = _get_tiling(shape=input_.shape, tile_shape=tile_shape, halo=halo, input_axes=input_.dims)

    assert all(isinstance(ax, str) for ax in input_.dims)
    input_axes: Tuple[str, ...] = input_.dims  # noqa

    # TODO need to adapt this that it supports out of core.
    # maybe xarray dask integration would help?
    # https://xarray.pydata.org/en/stable/user-guide/dask.html
    def load_tile(tile):
        inp = input_[tile]
        # whether to pad on the right or left of the dim for the spatial dims
        # + placeholders for batch and axis dimension, where we don't pad
        pad_right = [tile[ax].start == 0 if ax in "xyz" else None for ax in input_axes]
        return inp, pad_right

    if verbose:
        shape = {ax: sh for ax, sh in zip(prediction_pipeline.input_specs[0].axes, input_.shape)}
        n_tiles = int(np.prod([
            np.ceil(float(shape[ax]) / tsh) for ax, tsh in tile_shape.items()
        ]))
        tiles = tqdm(tiles, total=n_tiles, desc="prediction with tiling")

    # we need to use padded prediction for the individual tiles in case the
    # border tiles don't match the requested tile shape
    padding = {ax: tile_shape[ax] + 2 * halo[ax] for ax in input_axes if ax in "xyz"}
    padding["mode"] = "fixed"
    for outer_tile, inner_tile, local_tile in tiles:
        inp, pad_right = load_tile(outer_tile)
        out = predict_with_padding(prediction_pipeline, inp, padding, pad_right)
        assert len(out) == 1
        out = out[0]
        output[inner_tile] = out[local_tile]


#
# prediction functions
#


def predict(
    prediction_pipeline: PredictionPipeline,
    inputs: Union[xr.DataArray, List[xr.DataArray], Tuple[xr.DataArray]],
) -> List[xr.DataArray]:
    """Run prediction for a single set of input(s) with a bioimage.io model

    Args:
        prediction_pipeline: the prediction pipeline for the input model.
        inputs: the input(s) for this model represented as xarray data.
    """
    if not isinstance(inputs, (tuple, list)):
        inputs = [inputs]

    assert len(inputs) == len(prediction_pipeline.input_specs)
    tagged_data = [
        xr.DataArray(ipt, dims=ipt_spec.axes) for ipt, ipt_spec in zip(inputs, prediction_pipeline.input_specs)
    ]
    return prediction_pipeline.forward(*tagged_data)


def _parse_padding(padding, input_specs):
    if padding is None:  # no padding
        return padding
    if len(input_specs) > 1:
        raise NotImplementedError("Padding for multiple inputs not yet implemented")

    input_spec = input_specs[0]
    pad_keys = tuple(input_spec.axes) + ("mode",)

    def check_padding(padding):
        assert all(k in pad_keys for k in padding.keys())

    if isinstance(padding, dict):  # pre-defined padding
        check_padding(padding)
    elif isinstance(padding, bool):  # determine padding from spec
        if padding:
            axes = input_spec.axes
            shape = input_spec.shape
            if isinstance(shape, list):  # fixed padding
                padding = {ax: sh for ax, sh in zip(axes, shape) if ax in "xyz"}
                padding["mode"] = "fixed"
            else:  # dynamic padding
                step = shape.step
                padding = {ax: st for ax, st in zip(axes, step) if ax in "xyz"}
                padding["mode"] = "dynamic"
            check_padding(padding)
        else:  # no padding
            padding = None
    else:
        raise ValueError(f"Invalid argument for padding: {padding}")
    return padding


def predict_with_padding(
    prediction_pipeline: PredictionPipeline,
    inputs: Union[xr.DataArray, List[xr.DataArray], Tuple[xr.DataArray]],
    padding: Union[bool, Dict[str, int]],
    pad_right: bool = True,
) -> List[xr.DataArray]:
    """Run prediction with padding for a single set of input(s) with a bioimage.io model.

    Args:
        prediction_pipeline: the prediction pipeline for the input model.
        inputs: the input(s) for this model represented as xarray data.
        padding: the padding settings. Pass True to derive from the model spec.
        pad_right: whether to applying padding to the right or left of the input.
    """
    if not padding:
        raise ValueError
    assert len(inputs) == len(prediction_pipeline.input_specs)

    padding = _parse_padding(padding, prediction_pipeline.input_specs)
    if not isinstance(inputs, (tuple, list)):
        inputs = [inputs]
    if not isinstance(padding, (tuple, list)):
        padding = [padding]
    assert len(padding) == len(prediction_pipeline.input_specs)
    inputs, crops = zip(
        *[
            _pad(inp, spec.axes, p, pad_right=pad_right)
            for inp, spec, p in zip(inputs, prediction_pipeline.input_specs, padding)
        ]
    )

    result = predict(prediction_pipeline, inputs)
    return [_apply_crop(res, crop) for res, crop in zip(result, crops)]


# simple heuristic to determine suitable shape from min and step
def _determine_shape(min_shape, step, axes):
    is3d = "z" in axes
    min_len = 64 if is3d else 256
    shape = []
    for ax, min_ax, step_ax in zip(axes, min_shape, step):
        if ax in "zyx" and step_ax > 0:
            len_ax = min_ax
            while len_ax < min_len:
                len_ax += step_ax
            shape.append(len_ax)
        else:
            shape.append(min_ax)
    return shape


def _parse_tiling(tiling, input_specs, output_specs):
    if tiling is None:  # no tiling
        return tiling
    if len(input_specs) > 1:
        raise NotImplementedError("Tiling for multiple inputs not yet implemented")
    if len(output_specs) > 1:
        raise NotImplementedError("Tiling for multiple outputs not yet implemented")

    input_spec = input_specs[0]
    output_spec = output_specs[0]
    axes = input_spec.axes

    def check_tiling(tiling):
        assert "halo" in tiling and "tile" in tiling
        spatial_axes = [ax for ax in axes if ax in "xyz"]
        halo = tiling["halo"]
        tile = tiling["tile"]
        assert all(halo.get(ax, 0) > 0 for ax in spatial_axes)
        assert all(tile.get(ax, 0) > 0 for ax in spatial_axes)

    if isinstance(tiling, dict):
        check_tiling(tiling)
    elif isinstance(tiling, bool):
        if tiling:
            # NOTE we assume here that shape in input and output are the same
            # for different input and output shapes, we should actually tile in the
            # output space and then request the corresponding input tiles
            # so we would need to apply the output scale and offset to the
            # input shape to compute the tile size and halo here
            shape = input_spec.shape
            if not isinstance(shape, list):
                shape = _determine_shape(shape.min, shape.step, axes)
            assert isinstance(shape, list)
            assert len(shape) == len(axes)

            halo = output_spec.halo
            if halo is None:
                raise ValueError("Model does not provide a valid halo to use for tiling with default parameters")

            tiling = {
                "halo": {ax: ha for ax, ha in zip(axes, halo) if ax in "xyz"},
                "tile": {ax: sh for ax, sh in zip(axes, shape) if ax in "xyz"},
            }
            check_tiling(tiling)
        else:
            tiling = None
    else:
        raise ValueError(f"Invalid argument for tiling: {tiling}")
    return tiling


# TODO enable passing anything that is numpy array compatible, e.g. a zarr array
# Maybe use xarray dask integration? See https://xarray.pydata.org/en/stable/user-guide/dask.html
# TODO how do we do this with typing?
def predict_with_tiling(
    prediction_pipeline: PredictionPipeline,
    # TODO needs to be list, use Sequence instead of List / Tuple, allow numpy like
    inputs: Union[xr.DataArray, List[xr.DataArray], Tuple[xr.DataArray]],
    tiling: Union[bool, Dict[str, Dict[str, int]]],
    # TODO Sequence, allow numpy like
    outputs: Optional[Union[List[xr.DataArray]]] = None,
    verbose: bool = False,
) -> List[xr.DataArray]:
    """Run prediction with tiling for a single set of input(s) with a bioimage.io model.

    Args:
        prediction_pipeline: the prediction pipeline for the input model.
        inputs: the input(s) for this model represented as xarray data.
        tiling: the tiling settings. Pass True to derive from the model spec.
        outputs: optional output arrays.
        verbose: whether to print the prediction progress.
    """
    if not tiling:
        raise ValueError
    assert len(inputs) == len(prediction_pipeline.input_specs)

    tiling = _parse_tiling(tiling, prediction_pipeline.input_specs, prediction_pipeline.output_specs)
    if not isinstance(inputs, (list, tuple)):
        inputs = [inputs]
    named_inputs: OrderedDict[str, xr.DataArray] = collections.OrderedDict(
        **{
            ipt_spec.name: xr.DataArray(ipt_data, dims=tuple(ipt_spec.axes))
            for ipt_data, ipt_spec in zip(inputs, prediction_pipeline.input_specs)
        }
    )

    if outputs is None:
        outputs = []
        for output_spec in prediction_pipeline.output_specs:
            if isinstance(output_spec.shape, ImplicitOutputShape):
                scale = dict(zip(output_spec.axes, output_spec.shape.scale))
                offset = dict(zip(output_spec.axes, output_spec.shape.offset))

                # for now, we only support tiling if the spatial shape doesn't change
                # supporting this should not be so difficult, we would just need to apply the inverse
                # to "out_shape = scale * in_shape + 2 * offset" ("in_shape = (out_shape - 2 * offset) / scale")
                # to 'outer_tile' in 'get_tiling'
                if any(sc != 1 for ax, sc in scale.items() if ax in "xyz") or any(
                    off != 0 for ax, off in offset.items() if ax in "xyz"
                ):
                    raise NotImplementedError("Tiling with a different output shape is not yet supported")

                ref_input = named_inputs[output_spec.shape.reference_tensor]
                ref_input_shape = dict(zip(ref_input.dims, ref_input.shape))
                output_shape = tuple(int(scale[ax] * ref_input_shape[ax] + 2 * offset[ax]) for ax in output_spec.axes)
            else:
                output_shape = tuple(output_spec.shape)

            outputs.append(
                xr.DataArray(np.zeros(output_shape, dtype=output_spec.data_type), dims=tuple(output_spec.axes))
            )
    elif len(outputs) != len(prediction_pipeline.output_specs):
        raise ValueError(
            f"Number of outputs are incompatible: expected {len(prediction_pipeline.output_specs)}, got {len(outputs)}"
        )
    else:
        # eventually we need to fully validate the output shape against the spec, for now we only
        # support a single output of same spatial shape as the (single) input
        if len(outputs) != len(inputs):
            raise NotImplementedError("Tiling with a different number of inputs and outputs is not yet supported")
        spatial_in_shape = tuple(sh for ax, sh in zip(prediction_pipeline.input_specs[0].axes, inputs[0].shape))
        spatial_out_shape = tuple(sh for ax, sh in zip(prediction_pipeline.output_specs[0].axes, outputs[0].shape))
        if spatial_in_shape != spatial_out_shape:
            raise NotImplementedError("Tiling with a different output shape is not yet supported")

    _predict_with_tiling_impl(
        prediction_pipeline,
        list(named_inputs.values()),
        outputs,
        tile_shapes=[tiling["tile"]],  # todo: update tiling for multiple inputs/outputs
        halos=[tiling["halo"]],
        verbose=verbose,
    )

    return outputs


def _predict_sample(prediction_pipeline, inputs, outputs, padding, tiling):
    if padding and tiling:
        raise ValueError("Only one of padding or tiling is supported")

    input_data = _load_tensors(inputs, prediction_pipeline.input_specs)
    if padding is not None:
        result = predict_with_padding(prediction_pipeline, input_data, padding)
    elif tiling is not None:
        result = predict_with_tiling(prediction_pipeline, input_data, tiling)
    else:
        result = predict(prediction_pipeline, input_data)

    assert isinstance(result, list)
    assert len(result) == len(outputs)
    for res, out in zip(result, outputs):
        _save_image(out, res)


def predict_image(
    model_rdf: Union[RawResourceDescription, ResourceDescription, os.PathLike, str, dict, raw_nodes.URI],
    inputs: Union[Tuple[Path, ...], List[Path], Path],
    outputs: Union[Tuple[Path, ...], List[Path], Path],
    padding: Optional[Union[bool, Dict[str, int]]] = None,
    tiling: Optional[Union[bool, Dict[str, Dict[str, int]]]] = None,
    weight_format: Optional[str] = None,
    devices: Optional[List[str]] = None,
    verbose: bool = False,
):
    """Run prediction for a single set of input image(s) with a bioimage.io model.

    Args:
        model_rdf: the bioimageio model.
        inputs: the filepaths for the input images.
        outputs: the filepaths for saving the input images.
        padding: the padding settings for prediction. By default no padding is used.
        tiling: the tiling settings for prediction. By default no tiling is used.
        weight_format: the weight format to use for predictions.
        devices: the devices to use for prediction.
        verbose: run prediction in verbose mode.
    """
    if not isinstance(inputs, (tuple, list)):
        inputs = [inputs]

    if not isinstance(outputs, (tuple, list)):
        outputs = [outputs]

    model = load_resource_description(model_rdf)
    assert isinstance(model, Model)
    if len(model.inputs) != len(inputs):
        raise ValueError
    if len(model.outputs) != len(outputs):
        raise ValueError

    with create_prediction_pipeline(
        bioimageio_model=model, weight_format=weight_format, devices=devices
    ) as prediction_pipeline:
        _predict_sample(prediction_pipeline, inputs, outputs, padding, tiling)


def predict_images(
    model_rdf: Union[RawResourceDescription, ResourceDescription, os.PathLike, str, dict, raw_nodes.URI],
    inputs: Sequence[Union[Tuple[Path, ...], List[Path], Path]],
    outputs: Sequence[Union[Tuple[Path, ...], List[Path], Path]],
    padding: Optional[Union[bool, Dict[str, int]]] = None,
    tiling: Optional[Union[bool, Dict[str, Dict[str, int]]]] = None,
    weight_format: Optional[str] = None,
    devices: Optional[List[str]] = None,
    verbose: bool = False,
):
    """Predict multiple input images with a bioimage.io model.

    Args:
        model_rdf: the bioimageio model.
        inputs: the filepaths for the input images.
        outputs: the filepaths for saving the input images.
        padding: the padding settings for prediction. By default no padding is used.
        tiling: the tiling settings for prediction. By default no tiling is used.
        weight_format: the weight format to use for predictions.
        devices: the devices to use for prediction.
        verbose: run prediction in verbose mode.
    """

    model = load_resource_description(model_rdf)
    assert isinstance(model, Model)

    with create_prediction_pipeline(
        bioimageio_model=model, weight_format=weight_format, devices=devices
    ) as prediction_pipeline:

        prog = zip(inputs, outputs)
        if verbose:
            prog = tqdm(prog, total=len(inputs))

        for inp, outp in prog:
            if not isinstance(inp, (tuple, list)):
                inp = [inp]

            if not isinstance(outp, (tuple, list)):
                outp = [outp]

            _predict_sample(prediction_pipeline, inp, outp, padding, tiling)
