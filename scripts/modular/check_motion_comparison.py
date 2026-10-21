#!/usr/bin/env python3
"""Apply the reviewed numerical policy to float64 motion displacement only.

Inputs are readonly saved arrays and an independently frozen CSR equation system.
All other scientific comparisons retain their existing exact policy. This tool
never runs the estimator or changes an output. Run in the pinned scientific image.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import sparse

ATOL_UM = 1e-12
RTOL = 1e-12
MAX_BACKWARD_EPS = 8


def compare_displacements(reference, candidate, system, rhs):
    """Compare Motion's time-by-window arrays against the same independent Ax=b."""
    if type(reference) is not np.ndarray or type(candidate) is not np.ndarray:
        raise ValueError("Motion displacement inputs must be plain NumPy arrays")
    if reference.shape != candidate.shape or reference.ndim != 2 or not reference.size:
        raise ValueError("Motion displacement shapes must match and be nonempty time-by-window arrays")
    if reference.dtype != candidate.dtype or reference.dtype.kind != 'f' or reference.dtype.itemsize != 8:
        raise ValueError("Motion displacement dtypes must match and be float64")
    if not np.array_equal(np.isfinite(reference), np.isfinite(candidate)):
        raise ValueError("Motion displacement finite masks differ")
    if not np.isfinite(reference).all():
        raise ValueError("Motion displacement must be finite; the exception accepts no NaN or infinity")
    if not sparse.isspmatrix_csr(system) or system.shape != (reference.size, reference.size):
        raise ValueError("Independent motion equations must be a matching square CSR matrix")
    if system.dtype != np.dtype('float64') or not np.isfinite(system.data).all():
        raise ValueError("Independent motion matrix must be finite float64")
    if type(rhs) is not np.ndarray or rhs.shape != (reference.size,) or rhs.dtype != np.dtype('float64') or not np.isfinite(rhs).all():
        raise ValueError("Independent motion right-hand side must be a matching finite float64 vector")
    norm_a = float(abs(system).sum(axis=1).max())
    norm_b = float(np.max(np.abs(rhs)))

    def residual(array):
        # DREDGE's block system stacks each spatial window's complete time series.
        vector = array.T.ravel()
        error = np.asarray(system @ vector - rhs)
        maximum = float(np.max(np.abs(error)))
        denominator = norm_a * float(np.max(np.abs(vector))) + norm_b
        backward = maximum / denominator if denominator else (0.0 if maximum == 0 else float('inf'))
        if not np.isfinite(backward) or not np.isfinite(denominator):
            raise ValueError("Independent motion backward-error calculation is not finite")
        return dict(max_absolute_residual=maximum, scaled_backward_error=backward,
                    float64_epsilon_multiple=backward / np.finfo('float64').eps)

    left, right = residual(reference), residual(candidate)
    delta = np.abs(reference - candidate)
    within_tolerance = bool(np.allclose(candidate, reference, atol=ATOL_UM, rtol=RTOL, equal_nan=False))
    backward_passed = all(row['float64_epsilon_multiple'] <= MAX_BACKWARD_EPS for row in (left, right))
    return dict(passed=within_tolerance and backward_passed,
        policy='float64-motion-displacement-only/v1',absolute_tolerance_um=ATOL_UM,
        relative_tolerance=RTOL,maximum_backward_error_epsilon_multiple=MAX_BACKWARD_EPS,
        dtype=reference.dtype.str,shape=list(reference.shape),finite_masks_equal=True,all_finite=True,
        byte_identical=reference.tobytes(order='C') == candidate.tobytes(order='C'),
        within_tolerance=within_tolerance,backward_error_passed=backward_passed,
        reference=left,candidate=right,max_absolute_difference_um=float(delta.max()),
        per_window_max_absolute_difference_um=delta.max(axis=0).tolist(),
        per_window_differing_cells=np.count_nonzero(reference != candidate,axis=0).tolist(),
        reference_array_sha256=hashlib.sha256(reference.tobytes(order='C')).hexdigest(),
        candidate_array_sha256=hashlib.sha256(candidate.tobytes(order='C')).hexdigest(),
        equation_matrix_sha256=hashlib.sha256(system.data.tobytes()+system.indices.tobytes()+system.indptr.tobytes()).hexdigest(),
        equation_rhs_sha256=hashlib.sha256(rhs.tobytes()).hexdigest(),
        norm_a_inf=norm_a,norm_b_inf=norm_b,other_scientific_tolerances_changed=False)


def self_test():
    matrix=sparse.eye(2,format='csr',dtype='float64');reference=np.array([[1.0],[2.0]])
    rhs=np.array([1.0,2.0]);controls=[]
    exact=compare_displacements(reference,reference.copy(),matrix,rhs)
    assert exact['passed'] and exact['byte_identical'];controls.append('exact solution')
    changed=reference.copy();changed[0,0]=np.nextafter(changed[0,0],2.0)
    bounded=compare_displacements(reference,changed,matrix,rhs)
    assert bounded['passed'] and not bounded['byte_identical'];controls.append('bounded distinct solution')
    changed=reference.copy();changed[0,0]+=1e-13
    bad=compare_displacements(reference,changed,matrix,rhs)
    assert bad['within_tolerance'] and not bad['backward_error_passed'] and not bad['passed']
    controls.append('within tolerance but bad candidate backward error')
    bad=compare_displacements(changed,reference,matrix,rhs)
    assert bad['within_tolerance'] and not bad['passed'];controls.append('bad reference backward error')
    zeros=np.zeros((2,1));changed=zeros.copy();changed[1,0]=1e-8
    bad=compare_displacements(zeros,changed,sparse.diags([1.0,1e-30],format='csr'),np.zeros(2))
    assert bad['backward_error_passed'] and not bad['within_tolerance'] and not bad['passed']
    controls.append('good backward error but outside displacement tolerance')
    for name,left,right,needle in [
        ('shape',reference,reference.T,'shapes'),
        ('dtype',reference,reference.astype('float32'),'float64'),
        ('non-floating',reference.astype('int64'),reference.astype('int64'),'float64'),
        ('finite mask',reference,np.array([[np.nan],[2.0]]),'finite masks'),
        ('matching NaNs',np.full((2,1),np.nan),np.full((2,1),np.nan),'must be finite'),
        ('matching infinities',np.full((2,1),np.inf),np.full((2,1),np.inf),'must be finite'),
        ('masked nonfinite',np.ma.array([[np.nan],[2.0]],mask=[[True],[False]]),reference,'plain NumPy arrays')]:
        try:compare_displacements(left,right,matrix,rhs)
        except ValueError as error:assert needle in str(error),(name,error)
        else:raise AssertionError('Expected motion policy rejection: '+name)
        controls.append(name)
    return dict(passed=True,controls=controls,scientific_producer_executed=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test',action='store_true')
    for name in ['reference','candidate','system','rhs','output']:
        parser.add_argument('--'+name,type=Path)
    args=parser.parse_args()
    if args.self_test:
        result=self_test()
    else:
        if any(getattr(args,name) is None for name in ['reference','candidate','system','rhs','output']):
            parser.error('--reference, --candidate, --system, --rhs and --output are required')
        try:
            result=compare_displacements(np.load(args.reference,allow_pickle=False),
                np.load(args.candidate,allow_pickle=False),sparse.load_npz(args.system),np.load(args.rhs,allow_pickle=False))
        except (ValueError,OSError) as error:
            result=dict(passed=False,error=str(error),other_scientific_tolerances_changed=False)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps(result,indent=2,allow_nan=False))
    raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':
    main()
