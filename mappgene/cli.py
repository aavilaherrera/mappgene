#!/usr/bin/env python3
import argparse,parsl,os,sys,glob,shutil
from os.path import *
from mappgene.subscripts import *

script_dir = abspath(os.path.dirname(os.path.realpath(__file__)))
cwd = abspath(os.getcwd())

def parse_args(args):


    parser = argparse.ArgumentParser()

    if not '--test' in sys.argv:
        parser.add_argument('inputs', nargs='+',
            help='Paths to FASTQ input file(s).')

    parser.add_argument('--test', action='store_true',
        help='Test using the example inputs.')
    
    parser.add_argument('--dedup', '-D', action='store_true',
        help='Enable deduplication to drop the duplicated reads/pairs.')
    
    parser.add_argument('--threads', default=4, 
        help='Number of threads used by fastp filtering step.')
        
    parser.add_argument('--trim_front_tail', default=10,
        help='Number of NTs to remove from the start/end of read.')

    parser.add_argument('--outputs', '-o', default='mappgene_outputs/',
        help='Path to output directory.')

    parser.add_argument('--container', default=join(cwd, 'image.sif'),
        help='Path to Singularity container image.')

    parser.add_argument('--read-length', default=130,
        help='V-pipe: read length in sample.tsv (see cbg-ethz.github.io/V-pipe/tutorial/sars-cov2).')

    parser.add_argument('--variant_frequency', default=0.01,
        help='iVar: variant frequency cutoff.')

    parser.add_argument('--read_cutoff_bp', default=30,
        help='iVar: keep reads greater than this number of base pairs.')

    parser.add_argument('--primers_bp', default=400, choices={'400', '1200', 'v4', 'v4.1', 'combo_3_4.1', 400, 1200},
        help='iVar: use primer files with this number of base pairs.')

    parser.add_argument('--depth_cap', default='3e5',
        help='iVar: lofreq coverage depth cap.')

    scheduler_group = parser.add_mutually_exclusive_group()

    scheduler_group.add_argument('--slurm', action='store_true',
        help='Use the Slurm scheduler.')

    scheduler_group.add_argument('--flux', action='store_true',
        help='Use the Flux scheduler.')

    parser.add_argument('--nnodes', '-n', default=1,
        help='Slurm/Flux: number of nodes.')

    parser.add_argument('--use_full_node', action='store_true',
        help='Slurm/Flux: use entire node for each subject, disabling per-task memory management.')

    parser.add_argument('--bank', '-b', default='asccasc',
        help='Slurm/Flux: bank to charge for jobs.')

    parser.add_argument('--partition', '-p', default='pbatch',
        help='Slurm/Flux: partition to assign jobs.')

    parser.add_argument('--walltime', '-t', default='11:59:00',
        help='Slurm/Flux: walltime in format HH:MM:SS.')

    return parser.parse_args()

def main():

    args = parse_args(sys.argv[1:])


    tmp_dir = join(cwd, 'tmp')
    base_params = {
        'container': abspath(args.container),
        'work_dir': tmp_dir,
        'read_length': args.read_length,
        'variant_frequency': args.variant_frequency,
        'read_cutoff_bp': args.read_cutoff_bp,
        'dedup': args.dedup,
        'primers_bp': args.primers_bp,
        'depth_cap': float(args.depth_cap),
        'threads': int(args.threads),
        'trim_front_tail': int(args.trim_front_tail),
        'stdout': abspath(join(args.outputs, 'mappgene.stdout')),
    }

    if shutil.which('singularity') is None:
        raise Exception(f"Missing Singularity executable in PATH.\n\n" +
            f"Please ensure Singularity is installed: https://sylabs.io/guides/3.0/user-guide/installation.html")

    if not exists(base_params['container']):
        raise Exception(f"Missing container image at {base_params['container']}\n\n" +
            f"Either specify another image with --container\n" +
            f"Or build the container with the recipe at: {join(script_dir, 'data/container/recipe.def')}\n" +
            f"Or download the container with this command:\n\n$ singularity pull image.sif library://avilaherrera1/mappgene/image.sif:latest\n")

    smart_remove(tmp_dir)
    smart_mkdir(tmp_dir)
    smart_copy(join(script_dir, 'data/extra_files'), tmp_dir)
    
    update_permissions(tmp_dir, base_params)

    if args.test:
        args.inputs = join(script_dir, 'data/example_inputs/*.fastq.gz')
    
    if isinstance(args.inputs, str):
        args.inputs = glob(args.inputs)

    all_params = {}

    # Copy reads to subject directory
    for input_read in args.inputs:

        pair1 = input_read.replace('_R2', '_R1')
        pair2 = input_read.replace('_R1', '_R2')
        if input_read != pair1 and pair2 not in args.inputs:
            raise Exception(f'Missing paired read: {pair2}')
        if input_read != pair2 and pair1 not in args.inputs:
            raise Exception(f'Missing paired read: {pair1}')

        subject = (basename(input_read)
            .replace('.fastq.gz', '')
            .replace('.fastq', '')
            .replace('_R1', '')
            .replace('_R2', '')
            .replace('.', '_')
        )
        subject_dir = abspath(join(args.outputs, subject))

        if not subject in all_params:
            smart_copy(tmp_dir, subject_dir)
            params = base_params.copy()
            params['work_dir'] = subject_dir
            params['input_reads'] = [input_read]
            params['stdout'] = join(subject_dir, 'worker.stdout')
            all_params[subject] = params
        else:
            all_params[subject]['input_reads'].append(input_read)

    if args.use_full_node:
        cores_per_worker = int(os.cpu_count())
    else:
        cores_per_worker = 1
    
    # Memory management
    mem_per_worker = 0.1
    for subject in all_params:
        for input_read in all_params[subject]['input_reads']:
            fastq_size = 2.0 * os.path.getsize(input_read) * 1.0E-9
            mem_per_worker = max(mem_per_worker, fastq_size)

    if args.slurm:
        executor = parsl.executors.HighThroughputExecutor(
            label="worker",
            address=parsl.addresses.address_by_hostname(),
            cores_per_worker=cores_per_worker,
            mem_per_worker=mem_per_worker,
            provider=parsl.providers.SlurmProvider(
                args.partition,
                launcher=parsl.launchers.SrunLauncher(),
                nodes_per_block=int(args.nnodes),
                init_blocks=1,
                max_blocks=1,
                worker_init=f"export PYTHONPATH=$PYTHONPATH:{os.getcwd()}",
                walltime=args.walltime,
                scheduler_options="#SBATCH --exclusive\n#SBATCH -A {}\n".format(args.bank),
                move_files=False,
            ),
        )
    elif args.flux:
        executor = parsl.executors.FluxExecutor(
            label="worker",
            flux_path="/usr/global/tools/flux/toss_3_x86_64_ib/flux-c0.28.0.pre-s0.17.0.pre/bin/flux",
            cores_per_worker=cores_per_worker,
            mem_per_worker=mem_per_worker,
            provider=parsl.providers.SlurmProvider(
                args.partition,
                launcher=parsl.launchers.SrunLauncher(),
                nodes_per_block=int(args.nnodes),
                init_blocks=1,
                max_blocks=1,
                worker_init=f"export PYTHONPATH=$PYTHONPATH:{os.getcwd()}",
                walltime=args.walltime,
                scheduler_options="#SBATCH --exclusive\n#SBATCH -A {}\n".format(args.bank),
                move_files=False,
            ),
        )
    else:
        executor = parsl.executors.ThreadPoolExecutor(label="worker")

    config = parsl.config.Config(executors=[executor])
    parsl.set_stream_logger()
    parsl.load(config)

    results =  []
    for params in all_params.values():
        results.append(run_ivar(params))
    for r in results:
        r.result()

if __name__ == '__main__':
    main()

