"""Training functions for various multi-instance learning (MIL) models."""

import os
import numpy as np
import slideflow as sf
import pandas as pd
from os.path import join, exists
from typing import Union, List, Optional, TYPE_CHECKING
from slideflow import Dataset, log
from slideflow.util import path_to_name
from os.path import join, isdir

from .. import utils
from ..eval import predict_mil, generate_attention_heatmaps
from .._params import TrainerConfig

if TYPE_CHECKING:
    from fastai.learner import Learner


# -----------------------------------------------------------------------------

def train_mil(
    config: TrainerConfig,
    train_dataset: Dataset,
    val_dataset: Optional[Dataset],
    outcomes: Union[str, List[str]],
    bags: Union[str, List[str]],
    *,
    outdir: str = 'mil',
    exp_label: Optional[str] = None,
    **kwargs
) -> "Learner":
    """Train a multiple-instance learning (MIL) model.

    Args:
        config (:class:`slideflow.mil.TrainerConfig`):
            Trainer and model configuration.
        train_dataset (:class:`slideflow.Dataset`): Training dataset.
        val_dataset (:class:`slideflow.Dataset`): Validation dataset.
        outcomes (str): Outcome column (annotation header) from which to
            derive category labels.
        bags (str): Either a path to directory with \*.pt files, or a list
            of paths to individual \*.pt files. Each file should contain
            exported feature vectors, with each file containing all tile
            features for one patient.

    Keyword args:
        outdir (str): Directory in which to save model and results.
        exp_label (str): Experiment label, used for naming the subdirectory
            in the ``{project root}/mil`` folder, where training history
            and the model will be saved.
        attention_heatmaps (bool): Generate attention heatmaps for slides.
            Not available for multi-modal MIL models. Defaults to False.
        interpolation (str, optional): Interpolation strategy for smoothing
            attention heatmaps. Defaults to 'bicubic'.
        cmap (str, optional): Matplotlib colormap for heatmap. Can be any
            valid matplotlib colormap. Defaults to 'inferno'.
        norm (str, optional): Normalization strategy for assigning heatmap
            values to colors. Either 'two_slope', or any other valid value
            for the ``norm`` argument of ``matplotlib.pyplot.imshow``.
            If 'two_slope', normalizes values less than 0 and greater than 0
            separately. Defaults to None.

    """
    if not isinstance(config, TrainerConfig):
        raise ValueError(f"Unrecognized training configuration of type {type(config)}")

    return config.train(
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        outcomes=outcomes,
        bags=bags,
        outdir=outdir,
        exp_label=exp_label,
        **kwargs
    )

# -----------------------------------------------------------------------------

def build_fastai_learner(
    config: TrainerConfig,
    train_dataset: Dataset,
    val_dataset: Dataset,
    outcomes: Union[str, List[str]],
    bags: Union[str, np.ndarray, List[str]],
    *,
    outdir: str = 'mil',
    return_shape: bool = False,
    **kwargs
) -> "Learner":
    """Build a FastAI Learner for training an aMIL model.

    Args:
        train_dataset (:class:`slideflow.Dataset`): Training dataset.
        val_dataset (:class:`slideflow.Dataset`): Validation dataset.
        outcomes (str): Outcome column (annotation header) from which to
            derive category labels.
        bags (str): list of paths to individual \*.pt files. Each file should
            contain exported feature vectors, with each file containing all tile
            features for one patient.

    Keyword args:
        outdir (str): Directory in which to save model and results.
        exp_label (str): Experiment label, used for naming the subdirectory
            in the ``outdir`` folder, where training history
            and the model will be saved.
        lr (float): Learning rate, or maximum learning rate if
            ``fit_one_cycle=True``.
        epochs (int): Maximum epochs.
        **kwargs: Additional keyword arguments to pass to the FastAI learner.

    Returns:
        fastai.learner.Learner
    """
    from . import _fastai

    # Prepare labels and slides
    labels, unique_train = train_dataset.labels(outcomes, format='name')
    val_labels, unique_val = val_dataset.labels(outcomes, format='name')
    labels.update(val_labels)
    unique_categories = np.unique(unique_train + unique_val)

    # Prepare bags
    if isinstance(bags, str) or (isinstance(bags, list) and isdir(bags[0])):
        train_bags = train_dataset.get_bags(bags)
        if val_dataset is train_dataset:
            bags = train_bags
        else:
            val_bags = val_dataset.get_bags(bags)
            bags = np.concatenate((train_bags, val_bags))
    else:
        bags = np.array(bags)

    train_slides = train_dataset.slides()
    val_slides = val_dataset.slides()

    if config.aggregation_level == 'slide':
        # Aggregate feature bags across slides.

        bags, targets, train_idx, val_idx = utils.aggregate_trainval_bags_by_slide(
            bags,  # type: ignore
            labels,
            train_slides,
            val_slides,
            log_manifest=(join(outdir, 'slide_manifest.csv') if outdir else None)
        )

    elif config.aggregation_level == 'patient':
        # Associate patients and their slides.
        # This is a dictionary where each key is a slide name and each value
        # is a patient code. Multiple slides can match to the same patient.
        slide_to_patient = { **train_dataset.patients(),
                             **val_dataset.patients() }

        # Aggregate feature bags across patients.
        n_slide_bags = len(bags)
        bags, targets, train_idx, val_idx = utils.aggregate_trainval_bags_by_patient(
            bags,  # type: ignore
            labels,
            train_slides,
            val_slides,
            slide_to_patient=slide_to_patient,
            log_manifest=(join(outdir, 'slide_manifest.csv') if outdir else None)
        )
        log.info(f"Aggregated {n_slide_bags} slide bags to {len(bags)} patient bags.")

    log.info("Training dataset: {} merged bags (from {} possible slides)".format(
        len(train_idx), len(train_slides)))
    log.info("Validation dataset: {} merged bags (from {} possible slides)".format(
        len(val_idx), len(val_slides)))

    # Build FastAI Learner
    learner, (n_in, n_out) = _fastai.build_learner(
        config,
        bags=bags,
        targets=targets,
        train_idx=train_idx,
        val_idx=val_idx,
        unique_categories=unique_categories,
        outdir=outdir,
        **kwargs
    )
    if return_shape:
        return learner, (n_in, n_out)
    else:
        return learner


def build_multimodal_learner(
    config: TrainerConfig,
    train_dataset: Dataset,
    val_dataset: Dataset,
    outcomes: Union[str, List[str]],
    bags: Union[str, np.ndarray, List[str]],
    *,
    outdir: str = 'mil',
    return_shape: bool = False,
) -> "Learner":
    """Build a multi-magnification FastAI Learner for training an aMIL model."""

    from . import _fastai

    # Verify bags are in the correct format.
    if (not isinstance(bags, (tuple, list))
        or not all([isinstance(b, str) and isdir(b) for b in bags])):
        raise ValueError("Expected bags to be a list of paths, got {}".format(type(bags)))

    num_modes = len(bags)

    # Prepare labels and slides
    labels, unique_train = train_dataset.labels(outcomes, format='name')
    val_labels, unique_val = val_dataset.labels(outcomes, format='name')
    labels.update(val_labels)
    unique_categories = np.unique(unique_train + unique_val)

    # --- Prepare bags --------------------------------------------------------

    train_bags, train_slides = utils._get_nested_bags(train_dataset, bags)
    val_bags, val_slides = utils._get_nested_bags(val_dataset, bags)

    # --- Process bags and targets for training -------------------------------

    # Note: we are skipping patient-level bag aggregation for now.
    # TODO: implement patient-level bag aggregation for multi-modal MIL.

    # Concatenate training and validation bags.
    all_bags = np.concatenate((train_bags, val_bags)) # shape: (num_slides, num_modes)
    assert all_bags.shape[0] == len(train_slides) + len(val_slides)
    all_slides = train_slides + val_slides
    targets = np.array([labels[s] for s in all_slides])
    train_idx = np.arange(len(train_slides))
    val_idx = np.arange(len(train_slides), len(all_slides))

    # Write the slide manifest
    if outdir:
        sf.util.log_manifest(
            train_slides,
            val_slides,
            labels=labels,
            filename=join(outdir, 'slide_manifest.csv'),
            remove_extension=False
        )

    # Print a multi-modal dataset summary.
    log.info(
        "[bold]Multi-modal MIL training summary:[/]"
        + "\n  - [blue]Modes[/]: {}".format(num_modes)
        + "\n  - [blue]Slides with bags[/]: {}".format(len(np.unique(all_slides)))
        + "\n  - [blue]Multi-modal bags[/]: {}".format(all_bags.shape[0])
        + "\n  - [blue]Unique categories[/]: {}".format(len(unique_categories))
        + "\n  - [blue]Training multi-modal bags[/]: {}".format(len(train_idx))
        + "\n  - [blue]Training slides[/]: {}".format(len(np.unique(train_slides)))
        + "\n  - [blue]Validation multi-modal bags[/]: {}".format(len(val_idx))
        + "\n  - [blue]Validation slides[/]: {}".format(len(np.unique(val_slides)))
    )

    # Print a detailed summary of each mode.
    for i, mode in enumerate(bags):
        try:
            bags_config = sf.util.load_json(join(mode, 'bags_config.json'))
        except Exception:
            log.info(
                "Mode {i}: "
                + "\n  - Bags: {}".format(mode)
            )
        else:
            log.info(
                f"[bold]Mode {i+1}[/]: [green]{mode}[/]"
                + "\n  - Feature extractor: [purple]{}[/]".format(bags_config['extractor']['class'].split('.')[-1])
                + "\n  - Tile size (px): {}".format(bags_config['tile_px'])
                + "\n  - Tile size (um): {}".format(bags_config['tile_um'])
                + "\n  - Normalizer: {}".format(bags_config['normalizer'])
            )

    # --- Build FastAI Learner ------------------------------------------------

    # Build FastAI Learner
    learner, (n_in, n_out) = _fastai.build_learner(
        config,
        all_bags,
        targets,
        train_idx,
        val_idx,
        unique_categories,
        outdir=outdir,
    )
    if return_shape:
        return learner, (n_in, n_out)
    else:
        return learner


def train_fastai(
    config: TrainerConfig,
    train_dataset: Dataset,
    val_dataset: Dataset,
    outcomes: Union[str, List[str]],
    bags: Union[str, List[str]],
    *,
    outdir: str = 'mil',
    attention_heatmaps: bool = False,
    uq: bool = False,
    device: Optional[str] = None,
    **heatmap_kwargs
) -> "Learner":
    """Train an aMIL model using FastAI.

    Args:
        train_dataset (:class:`slideflow.Dataset`): Training dataset.
        val_dataset (:class:`slideflow.Dataset`): Validation dataset.
        outcomes (str): Outcome column (annotation header) from which to
            derive category labels.
        bags (str): Either a path to directory with \*.pt files, or a list
            of paths to individual \*.pt files. Each file should contain
            exported feature vectors, with each file containing all tile
            features for one patient.

    Keyword args:
        outdir (str): Directory in which to save model and results.
        exp_label (str): Experiment label, used for naming the subdirectory
            in the ``{project root}/mil`` folder, where training history
            and the model will be saved.
        lr (float): Learning rate, or maximum learning rate if
            ``fit_one_cycle=True``.
        epochs (int): Maximum epochs.
        attention_heatmaps (bool): Generate attention heatmaps for slides.
            Defaults to False.
        interpolation (str, optional): Interpolation strategy for smoothing
            attention heatmaps. Defaults to 'bicubic'.
        cmap (str, optional): Matplotlib colormap for heatmap. Can be any
            valid matplotlib colormap. Defaults to 'inferno'.
        norm (str, optional): Normalization strategy for assigning heatmap
            values to colors. Either 'two_slope', or any other valid value
            for the ``norm`` argument of ``matplotlib.pyplot.imshow``.
            If 'two_slope', normalizes values less than 0 and greater than 0
            separately. Defaults to None.

    Returns:
        fastai.learner.Learner
    """
    from . import _fastai

    # Prepare validation bags.
    if isinstance(bags, str) or (isinstance(bags, list) and isdir(bags[0])):
        val_bags = val_dataset.get_bags(bags)
    else:
        val_bags = np.array([b for b in bags if sf.util.path_to_name(b) in val_dataset.slides()])

    # Build learner.
    learner, (n_in, n_out) = build_fastai_learner(
        config,
        train_dataset,
        val_dataset,
        outcomes,
        bags=bags,
        outdir=outdir,
        device=device,
        return_shape=True
    )

    # Save MIL settings.
    # Attempt to read the unique categories from the learner.
    if not hasattr(learner.dls.train_ds, 'encoder'):
        unique = None
    else:
        encoder = learner.dls.train_ds.encoder
        if encoder is not None:
            unique = encoder.categories_[0].tolist()
        else:
            unique = None
    _log_mil_params(config, outcomes, unique, bags, n_in, n_out, outdir)

    # Train.
    _fastai.train(learner, config)

    # Generate validation predictions.
    df, attention = predict_mil(
        learner.model,
        config,
        dataset=val_dataset,
        outcomes=outcomes,
        bags=val_bags,
        attention=True,
        uq=uq,
    )
    if outdir:
        pred_out = join(outdir, 'predictions.parquet')
        df.to_parquet(pred_out)
        log.info(f"Predictions saved to [green]{pred_out}[/]")

    # Print categorical metrics, including per-category accuracy
    outcome_name = outcomes if isinstance(outcomes, str) else '-'.join(outcomes)
    df.rename(
        columns={c: f"{outcome_name}-{c}" for c in df.columns if c != 'slide'},
        inplace=True
    )
    sf.stats.metrics.categorical_metrics(df, level='slide', data_dir=outdir)

    # Export attention to numpy arrays
    if attention and outdir:
        utils._export_attention(
            join(outdir, 'attention'),
            attention,
            [path_to_name(b) for b in val_bags]
        )

    # Attention heatmaps.
    if attention and attention_heatmaps and outdir:
        generate_attention_heatmaps(
            outdir=join(outdir, 'heatmaps'),
            dataset=val_dataset,
            bags=val_bags,
            attention=attention,
            **heatmap_kwargs
        )

    return learner


def train_multimodal_mil(
    config: TrainerConfig,
    train_dataset: Dataset,
    val_dataset: Optional[Dataset],
    outcomes: Union[str, List[str]],
    bags: List[str],
    *,
    outdir: str = 'mil',
    exp_label: Optional[str] = None,
    attention_heatmaps: bool = False,
):
    """Train a multi-modal (e.g. multi-magnification) MIL model."""

    from . import _fastai

    # Export attention & heatmaps.
    if attention_heatmaps:
        raise ValueError(
                "Attention heatmaps cannot yet be exported for multi-modal "
                "models. Please use Slideflow Studio for visualization of "
                "multi-modal attention."
            )

    # Prepare validation bags and targets.
    val_labels, val_unique = val_dataset.labels(outcomes, format='id')
    val_bags, val_slides = utils._get_nested_bags(val_dataset, bags)
    val_targets = np.array([val_labels[slide] for slide in val_slides])

    # Build learner.
    learner, (n_in, n_out) = build_multimodal_learner(
        config,
        train_dataset,
        val_dataset,
        outcomes,
        bags=bags,
        outdir=outdir,
        return_shape=True
    )

    # Save MIL settings.
    # Attempt to read the unique categories from the learner.
    if not hasattr(learner.dls.train_ds, 'encoder'):
        unique = None
    else:
        encoder = learner.dls.train_ds.encoder
        if encoder is not None:
            unique = encoder.categories_[0].tolist()
        else:
            unique = None
    _log_mil_params(config, outcomes, unique, bags, n_in, n_out, outdir)

    # Execute training.
    _fastai.train(learner, config)

    # Generate validation predictions
    y_pred, y_att = sf.mil.eval._predict_multimodal_mil(
        learner.model,
        val_bags,
        attention=True,
        use_lens=config.model_config.use_lens
    )

    # Combine to a dataframe.
    df_dict = dict(slide=val_slides, y_true=val_targets)
    for i in range(y_pred.shape[-1]):
        df_dict[f'y_pred{i}'] = y_pred[:, i]
    df = pd.DataFrame(df_dict)

    # Print categorical metrics, including per-category accuracy
    outcome_name = outcomes if isinstance(outcomes, str) else '-'.join(outcomes)
    df.rename(
        columns={c: f"{outcome_name}-{c}" for c in df.columns if c != 'slide'},
        inplace=True
    )
    sf.stats.metrics.categorical_metrics(df, level='slide', data_dir=outdir)

    # Export predictions.
    if outdir:
        pred_out = join(outdir, 'predictions.parquet')
        df.to_parquet(pred_out)
        log.info(f"Predictions saved to [green]{pred_out}[/]")

    # Export attention.
    if y_att and outdir:
        utils._export_attention(join(outdir, 'attention'), y_att, df.slide.values)

    return learner

# ------------------------------------------------------------------------------

def _log_mil_params(config, outcomes, unique, bags, n_in, n_out, outdir=None):
    """Log MIL parameters to JSON."""
    mil_params = config.json_dump()
    mil_params['outcomes'] = outcomes
    if unique is not None:
        mil_params['outcome_labels'] = dict(zip(range(len(unique)), unique))
    else:
        mil_params['outcome_labels'] = None
    mil_params['bags'] = bags
    mil_params['input_shape'] = n_in
    mil_params['output_shape'] = n_out
    if isinstance(bags, str) and exists(join(bags, 'bags_config.json')):
        mil_params['bags_extractor'] = sf.util.load_json(
            join(bags, 'bags_config.json')
        )
    elif isinstance(bags, list):
        mil_params['bags_extractor'] = {}
        for b in bags:
            if isdir(b) and exists(join(b, 'bags_config.json')):
                mil_params['bags_extractor'][b] = sf.util.load_json(
                    join(b, 'bags_config.json')
                )
            else:
                mil_params['bags_extractor'][b] = None
    else:
        mil_params['bags_extractor'] = None
    if outdir:
        sf.util.write_json(mil_params, join(outdir, 'mil_params.json'))
    return mil_params