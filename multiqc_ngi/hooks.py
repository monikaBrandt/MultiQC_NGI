#!/usr/bin/env python
""" MultiQC hook functions - we tie into the MultiQC
core here to add in extra functionality. """

from __future__ import print_function
from collections import OrderedDict
from couchdb import Server
import logging
import json
import os
import re
import requests
import shutil
import socket
import sys
import yaml

import multiqc
from multiqc.utils import report, util_functions, config

log = logging.getLogger('multiqc')

report.ngi = dict()

# HOOK CLASS AND FUNCTIONS
class ngi_metadata():
    
    def __init__(self):
        
        # Check that these hooks haven't been disabled in the config file
        if getattr(config, 'disable_ngi', False) is True:
            return None

        if 'DuplicationMetrics' in report.data_sources.get('Picard', {}).keys() and 'FastQC' in report.data_sources.keys():
            for idx, mod in enumerate(report.general_stats_headers):
                if 'percent_duplicates' in mod.keys() and 'avg_sequence_length' in mod.keys():
                    report.general_stats_headers[idx]['percent_duplicates']['hidden'] = True
                    log.debug('Hiding FastQC % dups in General Stats as we have Picard MarkDups as well.')
                    break
        
        # Connect to StatusDB
        self.couch = self.connect_statusdb()
        if self.couch is not None:
            
            # Get project ID
            pid = None
            if 'project' in config.kwargs and config.kwargs['project'] is not None:
                log.info("Using supplied NGI project id: {}".format(config.kwargs['project']))
                pid = config.kwargs['project']
            else:
                pid = self.find_ngi_project()
            
            if pid is not None:
                # Get the metadata for the project
                self.get_ngi_project_metadata(pid)
                self.get_ngi_samples_metadata(pid)
                
                # Add to General Stats table
                self.general_stats_sample_meta()
                
                # Push MultiQC data to StatusDB
                if getattr(config, 'push_statusdb', None) is None:
                    config.push_statusdb = False
                if config.kwargs.get('push_statusdb', None) is not None:
                    config.push_statusdb = config.kwargs['push_statusdb']
                if config.push_statusdb:
                    self.push_statusdb_multiqc_data()
                else:
                    log.info("Not pushing results to StatusDB. To do this, use --push or set config push_statusdb: True")


    def find_ngi_project(self):
        """ Try to find a NGI project ID in the sample names.
        If just one found, add to the report header. """
        
        # Collect sample IDs
        self.s_names = set()
        for x in report.general_stats_data:
            self.s_names.update(x.keys())
        for d in report.saved_raw_data.values():
            self.s_names.update(d.keys())
        pids = set()
        for s_name in self.s_names:
            m = re.search(r'(P\d{3,5})', s_name)
            if m:
                pids.add(m.group(1))
        if len(pids) == 1:
            pid = pids.pop()
            log.info("Found one NGI project id: {}".format(pid))
            return pid
        elif len(pids) > 1:
            log.warn("Multiple NGI project IDs found! {}".format(",".join(pids)))
            return None
        else:
            log.info("No NGI project IDs found.")
            return None


    def get_ngi_project_metadata(self, pid):
        """ Get project metadata from statusdb """
        if self.couch is None:
            return None
        try:
            p_view = self.couch['projects'].view('project/summary')
        except socket.error:
            log.error('CouchDB Operation timed out')
        p_summary = None
        for row in p_view:
            if row['key'][1] == pid:
                p_summary = row
        
        try:
            p_summary = p_summary['value']
        except TypeError:
            log.error("statusdb returned no rows when querying {}".format(pid))
            return None
        
        log.debug("Found metadata for NGI project '{}'".format(p_summary['project_name']))
        
        config.title = '{}: {}'.format(pid, p_summary['project_name'])
        config.project_name = p_summary['project_name']
        config.output_fn_name = '{}_{}'.format(p_summary['project_name'], config.output_fn_name)
        config.data_dir_name = '{}_{}'.format(p_summary['project_name'], config.data_dir_name)
        log.debug("Renaming report filename to '{}'".format(config.output_fn_name))
        log.debug("Renaming data directory to '{}'".format(config.data_dir_name))
        
        report.ngi['pid'] = pid
        report.ngi['project_name'] = p_summary['project_name']
        keys = {
            'contact_email':'contact',
            'application': 'application'
        }
        d_keys = {
            'customer_project_reference': 'customer_project_reference',
            'project_type': 'type',
            'sequencing_platform': 'sequencing_platform',
            'sequencing_setup': 'sequencing_setup'
        }
        for i, j in keys.items():
            try:
                report.ngi[i] = p_summary[j]
            except KeyError:
                raise
        for i, j in d_keys.items():
            try:
                report.ngi[i] = p_summary['details'][j]
            except KeyError:
                raise


    def get_ngi_samples_metadata(self, pid):
        """ Get project sample metadata from statusdb """
        if self.couch is not None:
            p_view = self.couch['projects'].view('project/samples')
            p_samples = p_view[pid]
            if not len(p_samples.rows) == 1:
                log.error("statusdb returned {} rows when querying {}".format(len(p_samples.rows), pid))
            else:
                report.ngi['sample_meta'] = p_samples.rows[0]['value']
                report.ngi['ngi_names'] = dict()
                for s_name, s in report.ngi['sample_meta'].items():
                    report.ngi['ngi_names'][s_name] = s['customer_name']
                report.ngi['ngi_names_json'] = json.dumps(report.ngi['ngi_names'], indent=4)


    def general_stats_sample_meta(self):
            """ Add metadata about each sample to the General Stats table """
            
            meta = report.ngi['sample_meta']
            if meta is not None and len(meta) > 0:
                
                log.info('Found {} samples in StatusDB'.format(len(meta)))
                
                # Write to file
                util_functions.write_data_file(meta, 'ngi_meta')
                
                # Add to General Stats table
                gsdata = dict()
                formats = set()
                s_names = dict()
                conc_units = ''
                for sid in meta:
                    # Find first sample name matching this sample ID
                    s_name = sid
                    for x in sorted(self.s_names):
                        if sid in x:
                            s_name = x
                            s_names[s_name] = x
                            break
                    
                    # Create a dict with the data that we want
                    gsdata[s_name] = dict()
                    try:
                        gsdata[s_name]['initial_qc_conc'] = meta[sid]['initial_qc']['concentration']
                        formats.add(meta[sid]['initial_qc']['conc_units'])
                    except KeyError:
                        pass
                
                # Deal with having more than one initial QC concentration unit
                if len(formats) > 1:
                    log.warning("Mixture of initial QC concentration units! Found: {}".format(", ".join(formats)))
                    for s_name in gsdata:
                        try:
                            gsdata[s_name]['initial_qc_conc'] += ' '+meta[s_names[s_name]]['initial_qc']['conc_units']
                        except KeyError:
                            pass
                elif len(formats) == 1:
                    conc_units = formats.pop()
                
                gsheaders = OrderedDict()
                gsheaders['initial_qc_conc'] = {
                    'namespace': 'NGI',
                    'title': 'Conc. ({})'.format(conc_units),
                    'description': 'Initial QC Concentration ({})'.format(conc_units),
                    'min': 0,
                    'scale': 'YlGn',
                    'format': '{:.0f}'
                }
            
                report.general_stats_data.append(gsdata)
                report.general_stats_headers.append(gsheaders)
    
    
    def push_statusdb_multiqc_data(self):
        """ Push data parsed by MultiQC modules to the analysis database
        in statusdb. """
        
        # StatusDB view code for analysis/project_id view:
        # function(doc) {
        #   var project_id=Object.keys(doc.samples)[0].split('_')[0];
        #   emit(project_id, doc);
        # }
        
        # Connect to the analysis database
        if self.couch is None:
            return None
        try:
            db = self.couch['analysis']
            p_view = db.view('project/project_id')
        except socket.error:
            log.error('CouchDB Operation timed out')
            return None
        
        # Try to get an existing document if one exists
        doc = {}
        for row in p_view:
            if row['key'] == report.ngi['pid']:
                doc = row.value
                break
        
        # Start fresh unless the existing doc looks similar
        newdoc = {
            'entity_type': 'MultiQC_data',
            'project_id': report.ngi['pid'],
            'project_name': report.ngi['project_name'],
            'MultiQC_version': config.version,
            'MultiQC_NGI_version': config.multiqc_ngi_version,
        }
        for k in newdoc.keys():
            try:
                assert(doc[k] == newdoc[k])
            except (KeyError, AssertionError):
                doc = newdoc
                log.info('Creating new analysis record in StatusDB')
                break
        if doc != newdoc:
            log.info('Updating existing analysis record in StatusDB')
        
        # Add sample metadata to doc
        if 'samples' not in doc:
            doc['samples'] = dict()
        for key, d in report.saved_raw_data.items():
            for s_name in d:
                m = re.search(r'(P\d{3,5}_\d{1,6})', s_name)
                if m:
                    sid = m.group(1)
                else:
                    sid = s_name
                if sid not in doc['samples']:
                    doc['samples'][sid] = dict()
                doc['samples'][sid][key] = d[s_name]
        
        # Save object to the database
        db.save(doc)
    

    def connect_statusdb(self):
        """ Connect to statusdb """
        try:
            conf_file = os.path.join(os.environ.get('HOME'), '.ngi_config', 'statusdb.yaml')
            with open(conf_file, "r") as f:
                config = yaml.load(f)
        except IOError:
            log.warn("Could not open the MultiQC_NGI statusdb config file {}".format(conf_file))
            return None
        try:
            couch_user = config['statusdb']['username']
            password = config['statusdb']['password']
            couch_url = config['statusdb']['url']
            port = config['statusdb']['port']
        except KeyError:
            log.error("Error parsing the config file {}".format(conf_file))
            return None
        
        server_url = "http://{}:{}@{}:{}".format(couch_user, password, couch_url, port)
        
        # First, test that we can see the server.
        try:
            r = requests.get(server_url, timeout=3)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            log.warn("Cannot contact statusdb - skipping NGI metadata stuff")
            return None
        
        return Server(server_url)

