#!/usr/bin/env python
import subprocess
import sys
import traceback

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Union

import click

DEFAULT_VERBOSITY = 2
DEFAULT_BEAST_CONF = 'default.1mm.conf'
DEFAULT_TEMPLATE = 'mni_icbm152_t1_tal_nlin_sym_09c'

ENV_VAR_DPATH_SHARE = 'MNI_DATAPATH'
DNAME_BEAST_LIB = 'beast-library-1.1'
DNAME_TEMPLATE_MAP = {
    'mni_icbm152_t1_tal_nlin_sym_09c': 'icbm152_model_09c',
    'mni_icbm152_t1_tal_nlin_sym_09a': 'icbm152_model_09a',
}
SUFFIX_TEMPLATE_MASK = '_mask'

EXT_NIFTI = '.nii'
EXT_GZIP = '.gz'
EXT_MINC = '.mnc'
EXT_TRANSFORM = '.xfm'

SUFFIX_DENOISED = 'denoised'
SUFFIX_NORM = 'norm'
SUFFIX_MASK = 'mask'
SUFFIX_EXTRACTED = 'extracted'
SUFFIX_NONLINEAR = 'nl'
SUFFIX_DBM = 'dbm'
SUFFIX_RESHAPED = 'reshaped'

PREFIX_RUN = '[RUN] '
PREFIX_ERR = '[ERROR] '

# TODO wrapper function that considers BIDS things
# given a BIDS directory it computes DBM for all anatomical scans (?)
# and saves output according to BIDS standdard too (what would that be like?)
# TODO print start/end time
# TODO write stdout/stderr to log file (from wrapper function?)

@click.command()
@click.argument('fpath_nifti', type=str)
@click.argument('dpath_out', type=str, default='.')
@click.option('--share-dir', 'dpath_share', envvar=ENV_VAR_DPATH_SHARE,
              help='Path to directory containing BEaST library and '
                   f'anatomical models. Uses ${ENV_VAR_DPATH_SHARE} '
                   'environment variable if not specified.')
@click.option('--template-dir', 'dpath_templates',
              help='Directory containing anatomical templates.')
@click.option('--template', 'template_prefix', default=DEFAULT_TEMPLATE,
              help='Prefix for anatomical model files. '
                   f'Valid names: {list(DNAME_TEMPLATE_MAP.keys())}. '
                   f'Default: {DEFAULT_TEMPLATE}.')
@click.option('--beast-lib-dir', 'dpath_beast_lib', 
              help='Path to library directory for mincbeast.')
@click.option('--beast-conf', default=DEFAULT_BEAST_CONF,
              help='Name of configuration file for mincbeast. '
                   'Default: {DEFAULT_BEAST_CONF}.')
@click.option('--save-all/--save-subset', default=True,
              help='Save all intermediate files')
@click.option('--overwrite/--no-overwrite', default=False,
              help='Overwrite existing result files.')
@click.option('--dry-run/--no-dry-run', default=False,
              help='Print shell commands without executing them.')
@click.option('-v', '--verbose', 'verbosity', count=True, 
              default=DEFAULT_VERBOSITY,
              help='Set/increase verbosity level (cumulative). '
                   f'Default level: {DEFAULT_VERBOSITY}.')
@click.option('--quiet', is_flag=True, default=False,
              help='Suppress output whenever possible. '
                   'Has priority over -v/--verbose flags.')
def run_dbm_minc(fpath_nifti, dpath_out, dpath_share, 
                 dpath_templates, template_prefix, dpath_beast_lib, beast_conf, 
                 overwrite, save_all, dry_run, verbosity, quiet):

    def run_command(args, shell=False, stdout=None, stderr=None, silent=False):
        args = [str(arg) for arg in args if arg != '']
        args_str = ' '.join(args)
        if not silent and ((verbosity > 0) or dry_run):
            echo(f'{args_str}', prefix=PREFIX_RUN, text_color='yellow',
                 color_prefix_only=dry_run)
        if not dry_run:
            if stdout is None:
                if verbosity < 2:
                    stdout = subprocess.DEVNULL
                else:
                    stdout = None # TODO use log file
            if stderr is None:
                stderr = None # TODO use log file
            try:
                subprocess.run(args, check=True, shell=shell,
                               stdout=stdout, stderr=stderr)
            except subprocess.CalledProcessError as ex:
                print_error_and_exit(
                    f'Command {args_str} returned {ex.returncode}',
                    exit_code=ex.returncode,
                )

    def timestamp():
        run_command(['date'])

    try:

        timestamp()

        # overwrite if needed
        if quiet:
            verbosity = 0

        # process paths
        fpath_nifti = process_path(fpath_nifti)
        dpath_out = process_path(dpath_out)
        if dpath_share is None:
            if (dpath_templates is None) or (dpath_beast_lib is None):
                print_error_and_exit('If --share-dir is not given, '
                                     'both --template-dir and --beast-lib-dir '
                                     'must be specified.')
        else:
            dpath_share = process_path(dpath_share)
        if dpath_templates is None:
            dpath_templates = dpath_share / DNAME_TEMPLATE_MAP[template_prefix]
        else:
            dpath_templates = process_path(dpath_templates)
        if dpath_beast_lib is None:
            dpath_beast_lib = dpath_share / DNAME_BEAST_LIB
        else:
            dpath_beast_lib = process_path(dpath_beast_lib)

        # make sure input file exists and has valid extension
        if not fpath_nifti.exists():
            print_error_and_exit(f'Nifti file not found: {fpath_nifti}')
        valid_file_formats = (EXT_NIFTI, f'{EXT_NIFTI}{EXT_GZIP}')
        if not str(fpath_nifti).endswith(valid_file_formats):
            print_error_and_exit(
                f'Invalid file format for {fpath_nifti}'
                f'. Valid extensions are: {valid_file_formats}'
            )

        # generate paths for template files and make sure they are valid
        fpath_template = dpath_templates / f'{template_prefix}{EXT_MINC}'
        fpath_template_mask = add_suffix(fpath_template, 
                                         SUFFIX_TEMPLATE_MASK, sep=None)
        if not fpath_template.exists():
            print_error_and_exit(f'Template file not found: {fpath_template}')
        if not fpath_template_mask.exists():
            print_error_and_exit(
                f'Template mask file not found: {fpath_template_mask}'
            )

        # make sure beast library can be found
        if not dpath_beast_lib.exists():
            print_error_and_exit(
                f'BEaST library directory not found: {dpath_beast_lib}'
            )

        with TemporaryDirectory() as dpath_tmp:
            dpath_tmp = Path(dpath_tmp)

            # if zipped file, unzip
            if fpath_nifti.suffix == EXT_GZIP:
                fpath_raw_nii = dpath_tmp / fpath_nifti.stem # drop last suffix
                with fpath_raw_nii.open('wb') as file_raw:
                    run_command(['zcat', fpath_nifti], stdout=file_raw)
            # else create symbolic link
            else:
                fpath_raw_nii = dpath_tmp / fpath_nifti.name # keep last suffix
                run_command(['ln', '-s', fpath_nifti, fpath_raw_nii])

            # skip if output subdirectory already exists
            dpath_out_sub = dpath_out / fpath_raw_nii.stem
            try:
                dpath_out_sub.mkdir(parents=True, exist_ok=overwrite)
            except FileExistsError:
                if len(list(dpath_out_sub.iterdir())) != 0:
                    print_error_and_exit(
                        f'Non-empty output directory {dpath_out_sub} '
                        'already exists. Use --overwrite to overwrite.'
                    )

            # convert to minc format
            fpath_raw = dpath_tmp / fpath_raw_nii.with_suffix(EXT_MINC)
            run_command([
                'nii2mnc', 
                fpath_raw_nii, 
                fpath_raw,
            ])

            # denoise
            fpath_denoised = add_suffix(fpath_raw, SUFFIX_DENOISED)
            run_command([
                'mincnlm', 
                '-verbose',
                fpath_raw, 
                fpath_denoised,
            ])

            # normalize, scale, perform linear registration
            fpath_norm = add_suffix(fpath_denoised, SUFFIX_NORM)
            fpath_norm_transform = fpath_norm.with_suffix(EXT_TRANSFORM)
            run_command([
                'beast_normalize', 
                '-modeldir', dpath_templates,
                '-modelname', template_prefix,
                fpath_denoised, 
                fpath_norm, 
                fpath_norm_transform,
            ])

            # get brain mask
            fpath_mask = add_suffix(fpath_norm, SUFFIX_MASK)
            fpath_conf = dpath_beast_lib / beast_conf
            run_command([
                'mincbeast',
                '-fill',
                '-median',
                '-conf', fpath_conf,
                '-verbose',
                dpath_beast_lib,
                fpath_norm,
                fpath_mask,
            ])

            # extract brain
            fpath_extracted = add_suffix(fpath_norm, SUFFIX_EXTRACTED)
            run_command([
                'minccalc',
                '-verbose',
                '-expression', 'A[0]*A[1]',
                fpath_norm,
                fpath_mask,
                fpath_extracted,
            ])

            # perform nonlinear registration
            fpath_nonlinear = add_suffix(fpath_extracted, SUFFIX_NONLINEAR)
            fpath_nonlinear_transform = fpath_nonlinear.with_suffix(EXT_TRANSFORM)
            run_command([
                'nlfit_s',
                '-verbose',
                '-source_mask', fpath_mask,
                '-target_mask', fpath_template_mask,
                fpath_extracted,
                fpath_template,
                fpath_nonlinear_transform,
                fpath_nonlinear,
            ])

            # get DBM map
            fpath_dbm = add_suffix(fpath_nonlinear, SUFFIX_DBM)
            run_command([
                'pipeline_dbm.pl',
                '-verbose',
                '--model', fpath_template,
                fpath_nonlinear_transform,
                fpath_dbm,
            ])

            # need this otherwise nifti file has wrong affine
            fpath_dbm_reshaped = add_suffix(fpath_dbm, SUFFIX_RESHAPED)
            run_command([
                'mincreshape',
                '-dimorder', 'xspace,yspace,zspace',
                fpath_dbm,
                fpath_dbm_reshaped,
            ])

            # convert back to nifti
            fpath_dbm_nii = fpath_dbm_reshaped.with_suffix(EXT_NIFTI)
            run_command([
                'mnc2nii',
                '-nii',
                fpath_dbm_reshaped,
                fpath_dbm_nii,
            ])

            # list all output files
            run_command(['ls', '-lh', dpath_tmp])

            # copy all/some result files to output directory
            if save_all:
                fpaths_to_copy = dpath_tmp.iterdir()
            else:
                fpaths_to_copy = [
                    fpath_denoised,     # denoised
                    fpath_mask,         # brain mask
                    fpath_extracted,    # linearly registered
                    fpath_nonlinear,    # nonlinearly registered
                    fpath_dbm_nii,      # DBM map
                ]

            for fpath_source in fpaths_to_copy:
                run_command([
                    'cp', 
                    '-vfp', # verbose, force overwrite, preserve metadata
                    fpath_source, 
                    dpath_out_sub,
                ])

            # list files in output directory
            run_command(['ls', '-lh', dpath_out_sub])

            timestamp()

    except Exception:
        print_error_and_exit(traceback.format_exc())

def process_path(path: str) -> Path:
    return Path(path).expanduser().absolute()

def echo(message, prefix='', text_color=None, color_prefix_only=False):
    if (prefix != '') and (color_prefix_only):
        text = f'{click.style(prefix, fg=text_color)}{message}'
    else:
        text = click.style(f'{prefix}{message}', fg=text_color)
    click.echo(text, color=True)

def print_error_and_exit(message, text_color='red', exit_code=1):
    echo(message, prefix=PREFIX_ERR, text_color=text_color)
    sys.exit(exit_code)

def add_suffix(
    path: Union[Path, str], 
    suffix: str, 
    sep: Union[str, None] = '.',
) -> Path:
    if sep is not None:
        if suffix.startswith(sep):
            suffix = suffix[len(sep):]
    else:
        sep = ''
    path = Path(path)
    return path.parent / f'{path.stem}{sep}{suffix}{path.suffix}'

if __name__ == '__main__':
    run_dbm_minc()
