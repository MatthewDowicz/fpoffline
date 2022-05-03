import argparse
import logging
import pdb
import traceback
import sys
import pathlib
import datetime
import math
from ast import literal_eval as safe_eval

import numpy as np

import astropy.time

import desimeter
import desimeter.io
import desimeter.transform.ptl2fp
import desimeter.transform.pos2ptl
import desimeter.transform.xy2qs

import fpoffline.io
import fpoffline.const


def run(args):

    print(desimeter.__file__)

    # raise an exception here to flag any error
    # return value is propagated to the shell

    if args.night is None:
        # Use yesterday's date by default.
        args.night = int((datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y%m%d'))
        logging.info(f'Using default night {args.night}')

    # Check we have a valid parent path.
    if not args.parent_dir.exists():
        raise RuntimeError(f'Non-existent parent_dir: {args.parent_dir}')

    # Create a night subdirectory if necessary.
    output = args.parent_dir / str(args.night)
    if not output.exists():
        logging.info(f'Creating {output}')
        output.mkdir()

    # Calculate local midnight for this observing midnight.
    N = str(args.night)
    year, month, day = int(N[0:4]), int(N[4:6]), int(N[6:8])
    midnight = astropy.time.Time(datetime.datetime(year, month, day, 12) + datetime.timedelta(hours=19))
    logging.info(f'Local midnight on {N} is at {midnight}')

    # Calculate local noons before and after local midnight.
    twelve_hours = astropy.time.TimeDelta(12 * 3600, format='sec')
    noon_before = midnight - twelve_hours
    noon_after = midnight + twelve_hours

    # Load the most recent database snapshot.
    snapshot, snap_time = fpoffline.io.get_snapshot(astropy.time.Time(midnight, format='datetime'))
    snapshot['LOCATION'] = snapshot['PETAL_LOC']*1000 + snapshot['DEVICE_LOC']
    snapshot.sort('LOCATION')
    logging.info(f'Loaded snapshot {snapshot.meta["name"]}')
    snap_age = (midnight - snap_time).sec / 86400
    if snap_age > 0.6:
        logging.warning(f'Snapshot is {snap_age:.1f} days old.')

    # Initialize per-device summary table with metrology.
    metrology = desimeter.io.load_metrology() # returns an astropy table, not a pandas dataframe.
    summary = (metrology[np.isin(metrology['DEVICE_TYPE'],['POS','ETC','FIF','GIF'])][['LOCATION','X_FP','Y_FP']]
               .group_by('LOCATION').groups.aggregate(np.mean)) # use mean of fiducial locations
    summary.meta = {} # Do not use an OrderedDict
    summary.meta['night'] = args.night
    summary.meta['midnight'] = str(midnight)
    summary.meta['snapshot'] = snapshot.meta['name']
    summary.meta['snap_time'] = str(snap_time)

    # Add indexing info. The left join will drop 5 fidicuals that are missing metrology:
    # P074, P030, P029, P123, P058.
    I = fpoffline.io.get_index(args.night)
    print(f'Using index {I.meta["index_name"]}')
    I['LOCATION'] = 1000 * I['PETAL_LOC'] + I['DEVICE_LOC']
    summary = astropy.table.join(summary, I, keys='LOCATION', join_type='left')
    summary.sort('LOCATION')

    # Add calibration info from the snapshot.
    summary = reduce_snapshot(snapshot, summary)

    # Compute interpolated constants to transform from flatXY to ptlXY.
    s0 = np.hypot(summary['OFFSET_X'], summary['OFFSET_Y'])
    r0 = desimeter.transform.xy2qs.s2r(s0)
    summary['R0_OVER_S0'] = r0 / s0

    # Save the summary table as ECSV (so the metadata is included)
    # TODO: round float values
    summary.meta = dict(summary.meta) # Don't use an OrderedDict
    summary.write(output / f'fp-{args.night}.ecsv', overwrite=True)

    return 0


def reduce_snapshot(snapshot, summary):
    """Utility function used to merge snapshot calibration and keepout info into the summary table and its metadata.
    """
    canonical = lambda k: np.round(safe_eval(k), 3)

    cols = (
        'LENGTH_R1', 'LENGTH_R2', 'OFFSET_T', 'OFFSET_P', 'OFFSET_X', 'OFFSET_Y', 'PHYSICAL_RANGE_T', 'PHYSICAL_RANGE_P',
        'DEVICE_CLASSIFIED_NONFUNCTIONAL', 'FIBER_INTACT', 'LOCATION')
    reduced = astropy.table.Table(snapshot[cols])
    reduced.meta = {}
    reduced.sort('LOCATION')

    keepouts_t, keepout_t_idx = get_keepouts(snapshot, 'T', canonical)
    keepouts_p, keepout_p_idx = get_keepouts(snapshot, 'P', canonical)
    keepouts = dict(theta=keepouts_t.tolist(), phi=keepouts_p.tolist())

    # Transform GFA and PTL keepouts to focal-plane coords for each petal.
    gfa_x, gfa_y = desimeter.transform.pos2ptl.flat2ptl(
        *canonical(snapshot.meta['keepout_GFA']))
    ptl_x, ptl_y = desimeter.transform.pos2ptl.flat2ptl(
        *canonical(snapshot.meta['keepout_PTL']))
    gfa, ptl = [], []
    for petal_loc in range(10):
        x, y, _ = desimeter.transform.ptl2fp.ptl2fp(petal_loc, gfa_x, gfa_y)
        gfa.append(np.array([x, y]).tolist())
        x, y, _ = desimeter.transform.ptl2fp.ptl2fp(petal_loc, ptl_x, ptl_y)
        ptl.append(np.array([x, y]).tolist())
    keepouts['gfa'] = gfa
    keepouts['ptl'] = ptl

    # Save the indices of the keepouts used by each device.
    reduced['KEEPOUT_T'] = keepout_t_idx
    reduced['KEEPOUT_P'] = keepout_p_idx

    # Merge with the summary. The snapshot has no entries for the fidicuals, so their new fields
    # will be empty after the left join.
    summary = astropy.table.join(summary, reduced, keys='LOCATION', join_type='left')

    summary.meta['keepout'] = keepouts

    petals = []
    for petal_loc in range(10):
        petal_id = fpoffline.const.PETAL_ID_MAP[petal_loc]
        alignment = snapshot.meta['PETAL_ALIGNMENTS'][petal_id]
        gamma = alignment['gamma']
        petals.append(dict(
            Tx=alignment['Tx'], Ty=alignment['Ty'],
            cosGamma=math.cos(gamma), sinGamma=math.sin(gamma)))
    summary.meta['petals'] = petals

    return summary


def get_keepouts(snap, arm, canonical):

    nsnap = len(snap)
    keepouts = [ canonical(snap.meta[f'general_keepout_{arm}']) ]
    keepout_idx = np.full(nsnap, -1, int)

    for j, row in enumerate(snap):
        k = canonical(row[f'KEEPOUT_{arm}'])
        for i, ki in enumerate(keepouts):
            if np.array_equal(k, ki):
                keepout_idx[j] = i
                break
        if keepout_idx[j] == -1:
            keepout_idx[j] = len(keepouts)
            keepouts.append(k)

    logging.info(f'Found {len(keepouts)} unique {arm} keepouts for {nsnap} devices')
    return np.array(keepouts), keepout_idx


#def ptl2fp(x_ptl, y_ptl, petal_locs):
#    """Transform from petal flat (x,y) to focal-plane (x,y) with desimeter translate and rotate.
#    """
#    x_fp, y_fp = np.zeros(shape=(2,)+x_ptl.shape)
#    for petal_loc in np.unique(petal_locs):
#        sel = (petal_locs == petal_loc)
#        x_fp[sel], y_fp[sel], _ = desimeter.transform.ptl2fp.ptl2fp(
#            petal_loc, x_ptl[sel], y_ptl[sel])
#    return x_fp, y_fp


def main():
    # https://docs.python.org/3/howto/argparse.html
    parser = argparse.ArgumentParser(
        description='Run the focal-plane end of night analysis',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--night', type=str, metavar='YYYYMMDD',
        help='night to process or use the most recent night if not specified')
    parser.add_argument('--overwrite', action='store_true',
        help='overwrite any existing output files')
    parser.add_argument('--front-id', type=int, metavar='NNNNNNNN',
        help='exposure ID to use for the front-illuminated image')
    parser.add_argument('--back-id', type=int, metavar='NNNNNNNN',
        help='exposure ID to use for the back-illuminated image')
    parser.add_argument('--park-id', type=int, metavar='NNNNNNNN',
        help='exposure ID to use for the park robots script image')
    parser.add_argument('--parent-dir', type=pathlib.Path, metavar='PATH',
        default=pathlib.Path('/global/cfs/cdirs/desi/engineering/focalplane/endofnight'),
        help='parent directory for per-night output directories')
    parser.add_argument('-v', '--verbose', action='store_true',
        help='provide verbose output on progress')
    parser.add_argument('--debug', action='store_true',
        help='provide verbose and debugging output')
    parser.add_argument('--traceback', action='store_true',
        help='print traceback and enter debugger after an exception')
    args = parser.parse_args()

    # Configure logging.
    if args.debug:
        level = logging.DEBUG
    elif args.verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format='%(levelname)s %(message)s')

    try:
        retval = run(args)
        sys.exit(retval)
    except Exception as e:
        if args.traceback:
            # https://stackoverflow.com/questions/242485/starting-python-debugger-automatically-on-error
            extype, value, tb = sys.exc_info()
            traceback.print_exc()
            pdb.post_mortem(tb)
        else:
            print(e)
            sys.exit(-1)

if __name__ == '__main__':
    main()
