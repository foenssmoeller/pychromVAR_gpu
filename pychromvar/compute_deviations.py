from typing import Union
from anndata import AnnData
from mudata import MuData
import numpy as np
from scipy import sparse
from multiprocessing import Pool, cpu_count
import logging
from tqdm.auto import tqdm
import torch

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')


def compute_deviations(data: Union[AnnData, MuData], n_jobs=-1, chunk_size:int=10000, gpu = False) -> AnnData:
    """Compute raw and bias-corrected deviations.

    Parameters
    ----------
    data : Union[AnnData, MuData]
        AnnData object with peak counts or MuData object with 'atac' modality.
    n_jobs : int, optional
        Number of cpus used for motif matching. If set to -1, all cpus will be used. Default: -1.

    Returns
    -------
    Anndata
        An anndata object containing estimated deviations.
    """
    if isinstance(data, AnnData):
        adata = data
    elif isinstance(data, MuData) and "atac" in data.mod:
        adata = data.mod["atac"]
    else:
        raise TypeError(
            "Expected AnnData or MuData object with 'atac' modality")
    # check if the object contains bias in Anndata.varm
    assert "bg_peaks" in adata.varm_keys(
    ), "Cannot find background peaks in the input object, please first run get_bg_peaks!"
    logging.info('computing expectation reads per cell and peak...')
    expectation_obs, expectation_var = compute_expectation(count=adata.X, return_torch = gpu)
    logging.info('computing observed + bg motif deviations...')

    # compute background deviations for bias-correction
    n_bg_peaks = adata.varm['bg_peaks'].shape[1]
    if gpu:
        motif_match = torch.tensor(adata.varm['motif_match'], device = 'cuda', dtype=torch.float32)
        obs_dev = torch.zeros(size=(adata.n_obs, motif_match.shape[1]), dtype=torch.float32, device = 'cuda')
        bg_dev = torch.zeros(size=(n_bg_peaks, adata.n_obs, len(
            adata.uns['motif_name'])), dtype=torch.float32, device = 'cuda')
    else:
        motif_match = adata.varm['motif_match']
        obs_dev = np.zeros((adata.n_obs, motif_match.shape[1]), dtype=np.float32)
        bg_dev = np.zeros(shape=(n_bg_peaks, adata.n_obs, len(
            adata.uns['motif_name'])), dtype=np.float32)

    ### instead of iterating over bg peaks, iterate over X
    for item in tqdm(adata.chunked_X(chunk_size), position=0, leave=False, ncols=80, desc="rows"):
        X, start, end = item
        if gpu:
            if sparse.issparse(X):
                X = X.todense()
            X = torch.tensor(X, device = 'cuda', dtype=torch.float32)
            obs_dev[start:end, :] = _compute_deviations_gpu((motif_match, X, expectation_obs[start:end], expectation_var))
        else: 
            obs_dev[start:end, :] = _compute_deviations((motif_match, X, expectation_obs[start:end], expectation_var))

        for i in tqdm(range(n_bg_peaks), position=1, leave=False, ncols=80, desc="bg"):
            if gpu:
                bg_peak_idx = adata.varm['bg_peaks'][:, i]
                bg_motif_match = torch.tensor(adata.varm['motif_match'][bg_peak_idx, :], device = 'cuda', dtype=torch.float32)
                bg_dev[i, start:end, :] = _compute_deviations_gpu((bg_motif_match, X, expectation_obs[start:end], expectation_var))
            else:
                bg_peak_idx = adata.varm['bg_peaks'][:, i]
                bg_motif_match = adata.varm['motif_match'][bg_peak_idx, :]
                bg_dev[i, start:end, :] = _compute_deviations((bg_motif_match, X, expectation_obs[start:end], expectation_var))
    if gpu:
        mean_bg_dev = torch.mean(bg_dev, axis = 0)
        std_bg_dev = torch.std(bg_dev, axis=0)
        dev = (obs_dev - mean_bg_dev) / std_bg_dev
        mean_bg_dev = mean_bg_dev.cpu().numpy()
        std_bg_dev = std_bg_dev.cpu().numpy()
        dev = dev.cpu().numpy()

    else:
        mean_bg_dev = np.mean(bg_dev, axis=0)
        std_bg_dev = np.std(bg_dev, axis=0)
        dev = (obs_dev - mean_bg_dev) / std_bg_dev
    dev = np.nan_to_num(dev, 0)
    dev = AnnData(dev, dtype=np.float32)
    dev.obs_names = adata.obs_names
    dev.var_names = adata.uns['motif_name']
    return dev


def _compute_deviations(arguments):
    motif_match, count, expectation_obs, expectation_var = arguments
    ### motif_match: n_var x n_motif
    ### count, exp: n_obs x n_var
    observed = count.dot(motif_match)
    expected = expectation_obs.dot(expectation_var.dot(motif_match))
    if sparse.issparse(observed):
        observed = observed.todense()
    if sparse.issparse(expected):
        expected = expected.todense()
    out = np.zeros(expected.shape, dtype=expected.dtype)
    np.divide(observed - expected, expected, out=out, where=expected != 0)
    return out

def _compute_deviations_gpu(arguments):
    motif_match, count, expectation_obs, expectation_var = arguments
    ### motif_match: n_var x n_motif
    ### count, exp: n_obs x n_var
    observed = torch.matmul(count, motif_match)
    #observed = count.dot(motif_match)
    
    expected = torch.matmul(expectation_obs, torch.matmul(expectation_var, motif_match))
    #expected = expectation_obs.dot(expectation_var.dot(motif_match))
    #if sparse.issparse(observed):
    #    observed = observed.todense()
    #if sparse.issparse(expected):
    #    expected = expected.todense()
    out = torch.zeros(expected.shape, dtype=expected.dtype, device = 'cuda')
    torch.div(observed - expected, expected, out=out)
    out[expected == 0] = 0
    return out

def compute_expectation(count: Union[np.array, sparse.csr_matrix], return_torch = False) -> np.array:
    """
    Compute expetation accessibility per peak and per cell by assuming
    identical read probability per peak for each cell with a sequencing
    depth matched to that cell observed sequencing depth

    Parameters
    ----------
    count : Union[np.array, sparse.csr_matrix]
        Count matrix containing raw accessibility data.

    Returns
    -------
    np.array, np.array
        Expectation matrix pair when multiplied gives
    """
    a = np.asarray(count.sum(0), dtype=np.float32).reshape((1, count.shape[1]))
    a /= a.sum()
    b = np.asarray(count.sum(1), dtype=np.float32).reshape((count.shape[0], 1))
    if return_torch:
        return torch.tensor(b, device = 'cuda'), torch.tensor(a, device = 'cuda')
    else:
        return b, a
