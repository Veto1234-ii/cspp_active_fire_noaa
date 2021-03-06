#!/usr/bin/env python
# encoding: utf-8
"""
cspp_active_fire_noaa.py

 * DESCRIPTION: This is the main driver script for CSPP NOAA Active-Fire, through which ancillary
 data is transcoded, and 'vfire' is executed on the supplied input files.

Created by Geoff Cureton on 2017-01-03.
Copyright (c) 2017 University of Wisconsin Regents.
Licensed under GNU GPLv3.
"""

import os
import sys
import logging
import traceback
from cffi import FFI

from args import argument_parser
from active_fire_interface import get_afire_inputs, construct_cmd_invocations
from dispatcher import afire_dispatcher
from utils import create_dir, setup_cache_dir, clean_cache, cleanup, CsppEnvironment
from utils import check_and_convert_path, check_and_convert_env_var

os.environ['TZ'] = 'UTC'
ffi = FFI()

LOG = logging.getLogger(__name__)


def process_afire_inputs(work_dir, afire_options):
    """
    Construct dictionaries of valid input files and options, manage the ancillary cache, granulate
    the required ancillary data, and construct a series of command line invocations, which are then
    executed, returning the return codes for each valid input.
    """

    #ret_val = 0
    afire_home = afire_options['afire_home']
    geo_prefix = 'GITCO' if afire_options['i_band'] else 'GMTCO'

    attempted_runs = []
    successful_runs = []
    crashed_runs = []
    problem_runs = []

    LOG.info('')
    LOG.info('>>> Preparing inputs')
    LOG.info('')

    # Create a dictionary containing valid inputs and related metadata
    afire_data_dict, granule_id_list = get_afire_inputs(afire_options['inputs'], afire_options)

    for gran_key in afire_data_dict.keys():
        for file_key in afire_data_dict[gran_key].keys():
            LOG.debug('afire_data_dict["{}"]["{}"] = {}'.format(gran_key, file_key, afire_data_dict[gran_key][file_key]))

    if (afire_data_dict == {} or granule_id_list == []):
        LOG.info('>>> No valid {} inputs detected, aborting.'.format(
            'I-band' if afire_options['i_band'] else 'M-band'))
        return [],[],[],[]

    # Add the required command line invocations to the input dict...
    afire_data_dict = construct_cmd_invocations(afire_data_dict, afire_options)

    LOG.info('')
    LOG.info('>>> Input Files')
    LOG.info('')
    label_format_str = '{:^20s}{:^30s}'
    LOG.info(label_format_str.format("<Granule ID>", "<Granule Start Time>"))
    for granule_id in granule_id_list:
        LOG.info(label_format_str.format(granule_id,
                                         str(afire_data_dict[granule_id][geo_prefix]['dt'])))

    # Clean out product cache files that are too old.
    LOG.info('')
    if not afire_options['preserve_cache']:
        LOG.info(">>> Cleaning the ancillary cache back {} hours...".format(
            afire_options['cache_window']))
        first_dt = afire_data_dict[granule_id_list[0]][geo_prefix]['dt']
        clean_cache(afire_options['cache_dir'], afire_options['cache_window'], first_dt)

    # Create the required cache dirs
    for granule_id in granule_id_list:
        anc_dir = afire_data_dict[granule_id][geo_prefix]['dt'].strftime('%Y_%m_%d_%j-%Hh')
        lwm_dir = os.path.join(afire_options['cache_dir'], anc_dir)
        lwm_dir = create_dir(lwm_dir)
        if lwm_dir is None:
            LOG.warn("Unable to create cache dir {} for granule {}".format(lwm_dir, granule_id))

    # Run the dispatcher
    LOG.info('')
    LOG.info('>>> Running Active Fires')
    LOG.info('')
    rc_exe_dict, rc_problem_dict = afire_dispatcher(afire_home, afire_data_dict, afire_options)
    LOG.debug("rc_exe_dict = {}".format(rc_exe_dict))
    LOG.debug("rc_problem_dict = {}".format(rc_problem_dict))

    # Unless directed not to, cleanup the unaggregated inputs dir
    if afire_options['docleanup']:
        unagg_inputs_dir = os.path.join(work_dir, 'unaggregated_inputs')
        cleanup([unagg_inputs_dir])

    # Populate the diagnostic granule ID lists
    for granule_id in granule_id_list:
        attempted_runs.append(granule_id)
        if rc_exe_dict[granule_id] == 0:
            if rc_problem_dict[granule_id] == 0:
                successful_runs.append(granule_id)
            else:
                pass
        else:
            crashed_runs.append(granule_id)
        if rc_problem_dict[granule_id] != 0:
            problem_runs.append(granule_id)

    attempted_runs = sorted(list(set(attempted_runs)))
    successful_runs = sorted(list(set(successful_runs)))
    crashed_runs = sorted(list(set(crashed_runs)))
    problem_runs = sorted(list(set(problem_runs)))

    return attempted_runs, successful_runs, crashed_runs, problem_runs


def main():
    """
    The main method, which checks environment vars and collects all of the required input options.
    Returns 0 on success
    """

    # Read in the command line options
    args, work_dir, docleanup, cspp_afire_version, logfile = argument_parser()

    # Check various paths and environment variables that are "must haves".
    try:

        _, afire_home = check_and_convert_env_var('CSPP_ACTIVE_FIRE_HOME')
        _, afire_ancil_path = check_and_convert_env_var('CSPP_ACTIVE_FIRE_STATIC_DIR')
        _ = check_and_convert_path(None, os.path.join(afire_home, 'static_ancillary'),
                                   check_write=False)
        _ = check_and_convert_path(None, work_dir, check_write=False)

    except CsppEnvironment as e:
        LOG.error(e.value)
        LOG.error('Installation error, Make sure all software components were installed.')
        return 2

    afire_options = {}
    afire_options['inputs'] = args.inputs
    afire_options['afire_home'] = os.path.abspath(afire_home)
    afire_options['i_band'] = args.i_band
    afire_options['work_dir'] = os.path.abspath(args.work_dir)
    afire_options['ancil_dir'] = afire_ancil_path
    afire_options['cache_dir'] = setup_cache_dir(args.cache_dir, afire_options['work_dir'],
                                                 'CSPP_ACTIVE_FIRE_CACHE_DIR')
    afire_options['ancillary_only'] = args.ancillary_only
    afire_options['cache_window'] = args.cache_window
    afire_options['preserve_cache'] = args.preserve_cache
    afire_options['num_cpu'] = args.num_cpu
    afire_options['docleanup'] = docleanup
    afire_options['version'] = cspp_afire_version

    rc = 0
    try:

        attempted_runs, successful_runs, crashed_runs, problem_runs = process_afire_inputs(
            work_dir, afire_options)

        LOG.info('attempted_runs    {}'.format(attempted_runs))
        LOG.info('successful_runs   {}'.format(successful_runs))
        LOG.info('problem_runs      {}'.format(problem_runs))
        LOG.info('crashed_runs      {}'.format(crashed_runs))

    except Exception:
        LOG.error(traceback.format_exc())
        rc = 1

    return rc


if __name__ == '__main__':
    sys.exit(main())
