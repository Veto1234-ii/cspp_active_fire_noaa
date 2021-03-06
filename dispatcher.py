#!/usr/bin/env python
# encoding: utf-8
"""
dispatcher.py

 * DESCRIPTION: This file contains methods to construct a list of valid command line invocations,
 is the collection of utilities for parsing text, running external processes and
 binaries, checking environments and whatever other mundane tasks aren't specific to this project.

Created by Geoff Cureton on 2017-02-07.
Copyright (c) 2017 University of Wisconsin Regents.
Licensed under GNU GPLv3.
"""

import os
from os.path import basename, dirname, curdir, abspath, isdir, isfile, exists, splitext, join as pjoin
import logging
import time
import shutil
from glob import glob
import traceback
import multiprocessing
from datetime import datetime
from subprocess import call, check_call, CalledProcessError
import numpy as np

import h5py
from netCDF4 import Dataset

from utils import link_files, getURID, execution_time, execute_binary_captured_inject_io, cleanup

from ancillary.stage_ancillary import get_lwm

LOG = logging.getLogger('dispatcher')


def afire_submitter(args):
    '''
    This routine encapsulates the single unit of work, multiple instances of which are submitted to
    the multiprocessing queue. It takes as input whatever is required to complete the work unit,
    and returns return values and output logging from the external process.
    '''

    # This try block wraps all code in this worker function, to capture any exceptions.
    try:

        granule_dict = args['granule_dict']
        afire_home = args['afire_home']
        afire_options = args['afire_options']

        granule_id = granule_dict['granule_id']
        run_dir = granule_dict['run_dir']
        cmd = granule_dict['cmd']
        work_dir = afire_options['work_dir']
        env_vars = {}

        rc_exe = 0
        rc_problem = 0
        exe_out = "Finished the Active Fires granule {}".format(granule_id)

        LOG.debug("granule_id = {}".format(granule_id))
        LOG.debug("run_dir = {}".format(run_dir))
        LOG.debug("cmd = {}".format(cmd))
        LOG.debug("work_dir = {}".format(work_dir))
        LOG.debug("env_vars = {}".format(env_vars))

        current_dir = os.getcwd()

        LOG.info("Processing granule_id {}...".format(granule_id))

        # Create the run dir for this input file
        log_idx = 0
        while True:
            run_dir = pjoin(work_dir, "{}_run_{}".format(granule_dict['run_dir'], log_idx))
            if not exists(run_dir):
                os.makedirs(run_dir)
                break
            else:
                log_idx += 1

        os.chdir(run_dir)

        # Download and stage the required ancillary data for this input file
        LOG.info("\tStaging the required ancillary data for granule_id {}...".format(granule_id))
        failed_ancillary = False
        try:
            rc_ancil, rc_ancil_dict, lwm_file = get_lwm(afire_options, granule_dict)
            failed_ancillary = True if rc_ancil != 0 else False
        except Exception as err:
            failed_ancillary = True
            LOG.warn('\tProblem generating LWM for granule_id {}'.format(granule_id))
            LOG.error(err)
            LOG.debug(traceback.format_exc())

        # Run the active fire binary
        if afire_options['ancillary_only']:

            LOG.info('''\tAncillary only, skipping Active Fire execution for granule_id {}'''.format
                     (granule_id))
            if failed_ancillary:
                LOG.warn('\tAncillary granulation failed for granule_id {}'.format(granule_id))
                rc_problem = 1

            os.chdir(current_dir)

        elif failed_ancillary:

            LOG.warn('\tAncillary granulation failed for granule_id {}'.format(granule_id))
            os.chdir(current_dir)
            rc_problem = 1

        else:
            # Link the required files and directories into the work directory...
            paths_to_link = [
                pjoin(afire_home, 'vendor', afire_options['vfire_exe']),
                lwm_file,
            ] + [granule_dict[key]['file'] for key in afire_options['input_prefixes']]
            number_linked = link_files(run_dir, paths_to_link)
            LOG.debug("\tWe are linking {} files to the run dir:".format(number_linked))
            for linked_files in paths_to_link:
                LOG.debug("\t{}".format(linked_files))

            # Contruct a dictionary of error conditions which should be logged.
            error_keys = ['FAILURE', 'failure', 'FAILED', 'failed', 'FAIL', 'fail',
                          'ERROR', 'error', 'ERR', 'err',
                          'ABORTING', 'aborting', 'ABORT', 'abort']
            error_dict = {x: {'pattern': x, 'count_only': False, 'count': 0, 'max_count': None,
                              'log_str': ''}
                          for x in error_keys}
            error_dict['error_keys'] = error_keys

            start_time = time.time()

            rc_exe, exe_out = execute_binary_captured_inject_io(
                run_dir, cmd, error_dict,
                log_execution=False, log_stdout=False, log_stderr=False,
                **env_vars)

            end_time = time.time()

            afire_time = execution_time(start_time, end_time)
            LOG.debug("\tafire execution of {} took {:9.6f} seconds".format(
                granule_id, afire_time['delta']))
            LOG.info(
                "\tafire execution of {} took {} days, {} hours, {} minutes, {:8.6f} seconds"
                .format(granule_id, afire_time['days'], afire_time['hours'],
                        afire_time['minutes'], afire_time['seconds']))

            LOG.debug("\tGranule ID: {}, rc_exe = {}".format(granule_id, rc_exe))

            os.chdir(current_dir)

            # Write the afire output to a log file, and parse it to determine the output
            creation_dt = datetime.utcnow()
            timestamp = creation_dt.isoformat()
            logname = "{}_{}.log".format(run_dir, timestamp)
            log_dir = dirname(run_dir)
            logpath = pjoin(log_dir, logname)
            logfile_obj = open(logpath, 'w')
            for line in exe_out.splitlines():
                logfile_obj.write(str(line) + "\n")
            logfile_obj.close()

            # Update the various file global attributes
            try:

                old_output_file = pjoin(run_dir, granule_dict['AFEDR']['file'])
                creation_dt = granule_dict['creation_dt']

                # Check whether the target AF text file exists, and remove it.
                output_txt_file = '{}.txt'.format(splitext(old_output_file)[0])
                if exists(output_txt_file):
                    LOG.debug('{} exists, removing.'.format(output_txt_file))
                    os.remove(output_txt_file)

                #
                # Update the attributes, moving to the end
                #
                if afire_options['i_band']:

                    # Update the I-band attributes, and write the fire data to a text file.
                    h5_file_obj = h5py.File(old_output_file, "a")
                    h5_file_obj.attrs.create('date_created', np.string_(creation_dt.isoformat()))
                    h5_file_obj.attrs.create('granule_id', np.string_(granule_id))
                    history_string = 'CSPP Active Fires version: {}'.format(afire_options['version'])
                    h5_file_obj.attrs.create('history', np.string_(history_string))
                    h5_file_obj.attrs.create('Metadata_Link', np.string_(basename(old_output_file)))
                    h5_file_obj.attrs.create('id', np.string_(getURID(creation_dt)['URID']))

                    # Extract desired data from the NetCDF4 file, for output to the text file
                    nfire = h5_file_obj.attrs['FirePix'][0]
                    if int(nfire) > 0:
                        fire_datasets = ['FP_latitude', 'FP_longitude', 'FP_T4', 'FP_confidence',
                                         'FP_power']
                        fire_data = []
                        for dset in fire_datasets:
                            fire_data.append(h5_file_obj['/'+dset][:])

                    h5_file_obj.close()


                    # Check if there are any fire pixels, and write the associated fire data to
                    # a text file...

                    LOG.info("\tGranule {} has {} fire pixels".format(granule_id, nfire))

                    if int(nfire) > 0:
                        Along_scan_pixel_dim = 0.375
                        Along_track_pixel_dim = 0.375
                        fire_pixel_res = [Along_scan_pixel_dim, Along_track_pixel_dim]

                        format_str = '''{0:13.8f}, {1:13.8f}, {2:13.8f}, {5:6.3f}, {6:6.3f},''' \
                            ''' {3:4d}, {4:13.8f}'''

                        txt_file_header = \
                            '''# Active Fires I-band EDR\n''' \
                            '''#\n''' \
                            '''# source: {}\n''' \
                            '''# version: {}\n''' \
                            '''#\n''' \
                            '''# column 1: latitude of fire pixel (degrees)\n''' \
                            '''# column 2: longitude of fire pixel (degrees)\n''' \
                            '''# column 3: I04 brightness temperature of fire pixel (K)\n''' \
                            '''# column 4: Along-scan fire pixel resolution (km)\n''' \
                            '''# column 5: Along-track fire pixel resolution (km)\n''' \
                            '''# column 6: detection confidence ([7,8,9]->[lo,med,hi])\n''' \
                            '''# column 7: fire radiative power (MW)\n''' \
                            '''#\n# number of fire pixels: {}\n''' \
                            '''#'''.format(basename(old_output_file), history_string, nfire)

                        nasa_file = output_txt_file.replace('dev','dev_nasa')
                        if exists(nasa_file):
                            LOG.debug('{} exists, removing.'.format(nasa_file))
                            os.remove(nasa_file)

                        LOG.info("\tWriting output text file {}".format(output_txt_file))
                        txt_file_obj = open(output_txt_file, 'x')

                        try:
                            txt_file_obj.write(txt_file_header + "\n")

                            for FP_latitude, FP_longitude, FP_R13, FP_confidence, FP_power in zip(
                                    *fire_data):
                                fire_vars = [FP_latitude, FP_longitude, FP_R13, FP_confidence, FP_power]
                                line = format_str.format(*(fire_vars + fire_pixel_res))
                                txt_file_obj.write(line + "\n")

                            txt_file_obj.close()
                        except Exception:
                            txt_file_obj.close()
                            rc_problem = 1
                            LOG.warning("\tProblem writing Active fire text file: {}".format(
                                output_txt_file))
                            LOG.warn(traceback.format_exc())
                else:

                    # Update the M-band attributes, and write the fire data to a text file.

                    nc_file_obj = Dataset(old_output_file, "a", format="NETCDF4")
                    setattr(nc_file_obj, 'date_created', creation_dt.isoformat())
                    setattr(nc_file_obj, 'granule_id', granule_id)
                    history_string = 'CSPP Active Fires version: {}'.format(
                        afire_options['version'])
                    setattr(nc_file_obj, 'history', history_string)
                    setattr(nc_file_obj, 'Metadata_Link', basename(old_output_file))
                    setattr(nc_file_obj, 'id', getURID(creation_dt)['URID'])

                    # Extract desired data from the NetCDF4 file, for output to the text file
                    nfire = len(nc_file_obj['Fire Pixels'].dimensions['nfire'])
                    if int(nfire) > 0:
                        fire_datasets = ['FP_latitude', 'FP_longitude', 'FP_T13', 'FP_confidence',
                                         'FP_power']
                        fire_data = []
                        for dset in fire_datasets:
                            fire_data.append(nc_file_obj['Fire Pixels'].variables[dset][:])
                    nc_file_obj.close()

                    # Check if there are any fire pixels, and write the associated fire data to
                    # a text file...

                    LOG.info("\tGranule {} has {} fire pixels".format(granule_id, nfire))

                    if int(nfire) > 0:
                        Along_scan_pixel_dim = 0.75
                        Along_track_pixel_dim = 0.75
                        fire_pixel_res = [Along_scan_pixel_dim, Along_track_pixel_dim]

                        format_str = '''{0:13.8f}, {1:13.8f}, {2:13.8f}, {5:6.3f}, {6:6.3f},''' \
                            ''' {3:4d}, {4:13.8f}'''

                        txt_file_header = \
                            '''# Active Fires M-band EDR\n''' \
                            '''#\n''' \
                            '''# source: {}\n''' \
                            '''# version: {}\n''' \
                            '''#\n''' \
                            '''# column 1: latitude of fire pixel (degrees)\n''' \
                            '''# column 2: longitude of fire pixel (degrees)\n''' \
                            '''# column 3: M13 brightness temperature of fire pixel (K)\n''' \
                            '''# column 4: Along-scan fire pixel resolution (km)\n''' \
                            '''# column 5: Along-track fire pixel resolution (km)\n''' \
                            '''# column 6: detection confidence (%)\n''' \
                            '''# column 7: fire radiative power (MW)\n''' \
                            '''#\n# number of fire pixels: {}\n''' \
                            '''#'''.format(basename(old_output_file), history_string, nfire)

                        LOG.info("\tWriting output text file {}".format(output_txt_file))
                        txt_file_obj = open(output_txt_file, 'x')

                        try:
                            txt_file_obj.write(txt_file_header + "\n")

                            for FP_latitude, FP_longitude, FP_T13, FP_confidence, FP_power in zip(
                                    *fire_data):
                                fire_vars = [FP_latitude, FP_longitude, FP_T13, FP_confidence, FP_power]
                                line = format_str.format(*(fire_vars + fire_pixel_res))
                                txt_file_obj.write(line + "\n")

                            txt_file_obj.close()
                        except Exception:
                            txt_file_obj.close()
                            rc_problem = 1
                            LOG.warning("\tProblem writing Active fire text file: {}".format(
                                output_txt_file))
                            LOG.warn(traceback.format_exc())

            except Exception:
                rc_problem = 1
                LOG.warning("\tProblem setting attributes in output file {}".format(
                    old_output_file))
                LOG.debug(traceback.format_exc())

            # Move output files to the work directory
            LOG.debug("\tMoving output files from {} to {}".format(run_dir, work_dir))
            af_prefix = 'AFIMG' if afire_options['i_band'] else 'AFMOD'
            af_suffix = 'nc' if afire_options['i_band'] else 'nc' # FIXME: NOAA should fix NC output for I-band!
            outfiles = glob(pjoin(run_dir, '{}*.{}'.format(af_prefix, af_suffix))) \
                     + glob(pjoin(run_dir, '{}*.txt'.format(af_prefix)))

            for outfile in outfiles:
                try:
                    shutil.move(outfile, work_dir)
                except Exception:
                    rc_problem = 1
                    LOG.warning("\tProblem moving output {} from {} to {}".format(
                        outfile, run_dir, work_dir))
                    LOG.debug(traceback.format_exc())

        # If no problems, remove the run dir
        if (rc_exe == 0) and (rc_problem == 0) and afire_options['docleanup']:
                cleanup([run_dir])

    except Exception:
        LOG.warn("\tGeneral warning for {}".format(granule_id))
        LOG.debug(traceback.format_exc())
        os.chdir(current_dir)
        #raise

    return [granule_id, rc_exe, rc_problem, exe_out]


def afire_dispatcher(afire_home, afire_data_dict, afire_options):
    """
    Dispatch one or more Active Fires jobs to the multiprocessing pool, and report back the final
    job statuses.
    """

    # Construct a list of task dicts...
    granule_id_list = sorted(afire_data_dict.keys())
    afire_tasks = []
    for granule_id in granule_id_list:
        args = {'granule_dict': afire_data_dict[granule_id],
                'afire_home': afire_home,
                'afire_options': afire_options}
        afire_tasks.append(args)

    # Setup the processing pool
    cpu_count = multiprocessing.cpu_count()
    LOG.info('There are {} available CPUs'.format(cpu_count))

    requested_cpu_count = afire_options['num_cpu']

    if requested_cpu_count is not None:
        LOG.info('We have requested {} {}'.format(requested_cpu_count,
                                                  "CPU" if requested_cpu_count == 1 else "CPUs"))

        if requested_cpu_count > cpu_count:
            LOG.warn('{} requested CPUs is greater than available, using {}'.format(
                requested_cpu_count, cpu_count))
            cpus_to_use = cpu_count
        else:
            cpus_to_use = requested_cpu_count
    else:
        cpus_to_use = cpu_count

    LOG.info('We are using {}/{} available CPUs'.format(cpus_to_use, cpu_count))
    pool = multiprocessing.Pool(cpus_to_use)

    # Submit the Active Fire tasks to the processing pool
    timeout = 9999999
    result_list = []

    start_time = time.time()

    LOG.info("Submitting {} Active Fire {} to the pool...".format(
        len(afire_tasks), "task" if len(afire_tasks) == 1 else "tasks"))
    result_list = pool.map_async(afire_submitter, afire_tasks).get(timeout)

    end_time = time.time()

    total_afire_time = execution_time(start_time, end_time)
    LOG.debug("afire execution took {:9.6f} seconds".format(total_afire_time['delta']))
    LOG.info(
        "Active Fire execution took {} days, {} hours, {} minutes, {:8.6f} seconds"
        .format(total_afire_time['days'], total_afire_time['hours'],
                total_afire_time['minutes'], total_afire_time['seconds']))
    LOG.info('')

    rc_exe_dict = {}
    rc_problem_dict = {}

    # Loop through each of the Active Fire results collect error information
    for result in result_list:
        granule_id, afire_rc, problem_rc, exe_out = result
        LOG.debug(">>> granule_id {}: afire_rc = {}, problem_rc = {}".format(
            granule_id, afire_rc, problem_rc))

        # Did the actual afire binary succeed?
        rc_exe_dict[granule_id] = afire_rc
        rc_problem_dict[granule_id] = problem_rc

    return rc_exe_dict, rc_problem_dict


# Some information about simulating an exe segfault.
'''
To deliberately throw a segfault for testing, we can set...

    cmd = 'echo "This is a test cmd to throw a segfault..." ; kill -11 $$'

or compile a custom C exe...

    echo "int main() { *((char *)0) = 0; }" > segfault_get.c
    gcc segfault_get.c -o segfault_get

and then set...

    cmd = '/mnt/WORK/work_dir/test_data/sample_data/segfault_get'

which should generate a return code of -11 (segfault).
'''

# A couple of commands which fail to produce output...
#sat_obj.cmd['seg_2'] = 'sleep 0.5'
#sat_obj.cmd['seg_2'] = 'sleep 0.5; exit 1'
#sat_obj.cmd = {x:'sleep 0.5; exit 1' for x in sat_obj.segment_data['segment_keys']}
#sat_obj.cmd['seg_2'] = '/mnt/WORK/work_dir/segfault_test/segfault_get'
#sat_obj.cmd = {x:'sleep 0.5;
#    echo "geocat>> Cannot create HDF writing for SDS, cloud_spherical_albedo - aborting."'
#    for x in sat_obj.segment_data['segment_keys']}
