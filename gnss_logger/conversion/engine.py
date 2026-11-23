"""Run an external convbin on an isolated snapshot and publish validated files."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from ..capture.framing import Framer
from ..device.ubx import rawx, sfrbx
from ..session import file_fingerprint
from .config import COMBINED, GNSS, GROUPS, MASKS, SIGNALS
from .rinex import observations, navigation
from .results import OBS_ONLY, commit, existing, identity, legacy

def invoke(args, cwd, timeout):
    # convbin can write progress for every epoch. Limit the size retained in JSON.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(args,cwd=cwd,shell=False,stdin=subprocess.DEVNULL,
                                    stdout=stdout,stderr=stderr,timeout=timeout)
            code=result.returncode
        except subprocess.TimeoutExpired:
            code=None
        logs={}
        for name, handle in (('stdout',stdout),('stderr',stderr)):
            size=handle.tell()
            handle.seek(max(0,size-65536))
            logs[name]=handle.read().decode('utf-8',errors='replace')
            logs[name+'_truncated']=size>65536
        return dict(returncode=code,timed_out=code is None,**logs)


def probe(settings, identity):
    exe=settings.executable
    if not exe.is_file():
        raise ValueError('convbin executable not found: '+str(exe))
    if exe.suffix.lower() in ('.cmd','.bat'):
        raise ValueError('Select a native convbin executable, not a shell wrapper')
    identity.update(path=str(exe),**file_fingerprint(exe))
    with tempfile.TemporaryDirectory(prefix='gnss-convbin-probe-') as cwd:
        help_result=invoke([str(exe)],cwd,10)
        help_text=help_result['stdout']+'\n'+help_result['stderr']
        if not re.search(r'(?m)^\s+-mask\b',help_text):
            help_result=invoke([str(exe),'--help'],cwd,10)
            help_text=help_result['stdout']+'\n'+help_result['stderr']
        options=set(re.findall(r'(?m)^\s+(-[a-z][a-z0-9-]*)(?=\s|$)',help_text))
        identity.update(help=help_result,options=sorted(options))
        required={'-r','-v','-f','-mask','-y'}
        if settings.obs:
            required.add('-o')
            required.update({'D':'-od','S':'-os'}[c] for c in settings.observations if c in 'DS')
            if settings.receiver_model:
                required.add('-hr')
        if settings.nav:
            required.add('-n')
        missing=sorted(required-options)
        if missing or not re.search(r'\bubx\b',help_text,re.I):
            raise ValueError('Unsupported convbin interface. Missing: '+', '.join(missing or ['UBX']))
        # Some older builds print help for this unknown switch.
        try:
            version=invoke([str(exe),'--version'],cwd,10)
            version_text=(version['stdout']+'\n'+version['stderr']).strip()
            identity['version']=version_text if (version['returncode']==0
                and len(version_text.splitlines())==1 and 'rtklib' in version_text.lower()) else None
        except subprocess.TimeoutExpired:
            identity['version']=None


def command(settings, source, options):
    model=settings.receiver_model or source.receiver_model
    args=[str(settings.executable),'-r','ubx','-v',settings.version,
          '-f',str(max(MASKS[mask] for mask in settings.masks))]
    args.extend(('-mask',','.join(sorted(settings.masks))))
    for system in 'GRECJSI':
        if system not in settings.systems:
            args.extend(('-y',system))
    if settings.obs:
        args.extend(('-o','output.obs'))
        args.extend({'D':'-od','S':'-os'}[c] for c in settings.observations if c in 'DS')
        if model:
            if '-hr' in options:
                args.extend(('-hr','UNKNOWN/'+model+'/UNKNOWN'))
            elif settings.receiver_model:
                raise ValueError('convbin lacks requested -hr receiver header option')
    if settings.nav:
        args.extend(('-n','output.nav'))
    return args+['input.ubx']


def snapshot(source, target):
    framer=Framer()
    digest=hashlib.sha256()
    signals=Counter()
    nav=Counter()
    epochs=0
    opaque=0
    invalid=0
    with source.path.open('rb') as incoming, target.open('xb') as outgoing:
        while block:=incoming.read(16384):
            if outgoing.write(block)!=len(block):
                raise OSError('Short write while creating immutable conversion snapshot')
            digest.update(block)
            for _, original, frame in framer.feed(block):
                if frame is None:
                    opaque+=len(original)
                elif frame[:2]==(2,0x15):
                    try:
                        epoch=rawx(frame[2])
                        epochs+=1
                        for m in epoch['measurements']:
                            if m['code_valid'] or m['phase_valid']:
                                signals[(m['gnss_id'],m['sig_id'])]+=1
                    except ValueError:
                        invalid+=1
                elif frame[:2]==(2,0x13):
                    try:
                        nav[sfrbx(frame[2])['gnss_id']]+=1
                    except ValueError:
                        invalid+=1
    size=target.stat().st_size
    sha=digest.hexdigest()
    if not size or (source.size is not None and (size!=source.size or sha!=source.sha256)):
        raise ValueError('Source differs from completed artifact fingerprint')
    return dict(bytes=size,sha256=sha,rawx_epochs=epochs,opaque_bytes=opaque,
                tail_bytes=len(framer.tail()[1]),decode_errors=invalid,
                rawx_signals={f'{g}:{s}':n for (g,s),n in signals.items()},
                sfrbx_systems={GNSS[g]:n for g,n in nav.items() if g in GNSS})


def stage_report(settings, source, root, options):
    outputs={key:str(root/(source.path.stem+'.'+key)) for key in ('obs','nav')
             if getattr(settings,key)}
    work=root/('.convbin-'+uuid.uuid4().hex)
    return dict(source={**asdict(source),'path':str(source.path)},outputs=outputs,
                cwd=str(work),args=command(settings,source,options) if options is not None else [],
                validation={},published_outputs={},warnings=[],errors=[])


def convert(settings, source, report, dry_run, engine):
    work=Path(report['cwd'])
    outputs={key:Path(value) for key,value in report['outputs'].items()}
    report['status']='FAILED'
    if source.path.suffix.lower()!='.ubx' or source.path.is_symlink() or not source.path.is_file():
        raise ValueError('Expected a completed regular .ubx file: '+str(source.path))
    receipt=work.parent/(source.path.stem+'.conversion.json')
    occupied=os.path.lexists(receipt) or any(os.path.lexists(path) for path in outputs.values())
    if dry_run or occupied:
        fingerprint=file_fingerprint(source.path)
        if not fingerprint['bytes']:
            raise ValueError('Empty UBX: '+str(source.path))
        if source.size is not None and fingerprint!=dict(bytes=source.size,sha256=source.sha256):
            raise ValueError('Source differs from completed artifact fingerprint')
        report['source'].update(fingerprint)
    if occupied:
        key=identity(settings,report,engine)
        prior=existing(receipt,key,report['outputs'])
        result_path=str(receipt)
        if prior is None and not os.path.lexists(receipt):
            prior,result_path=legacy(work.parent,key,report['outputs'])
        if prior is None:
            report['status']='CONFLICT'
            report['errors'].append('Existing output without a matching verified conversion result')
        else:
            report.update(status=OBS_ONLY if prior['status']==OBS_ONLY else 'ALREADY_CONVERTED',result_path=result_path,
                          outputs=prior['published_outputs'].copy(),
                          published_outputs=prior['published_outputs'],
                          output_fingerprints=prior['output_fingerprints'],
                          validation=prior['validation'],warnings=prior['warnings'])
            if prior['status']==OBS_ONLY:
                report['missing_outputs']=prior['missing_outputs']
                report['requested_outputs']=prior['requested_outputs']
        return
    if dry_run:
        report['status']='DRY_RUN'
        return
    with tempfile.TemporaryDirectory(prefix=work.name+'-',dir=work.parent) as folder:
        work=Path(folder)
        report['cwd']=folder
        fingerprint=snapshot(source,work/'input.ubx')
        report['source'].update(fingerprint)
        if fingerprint['opaque_bytes'] or fingerprint['tail_bytes'] or fingerprint['decode_errors']:
            raise ValueError('UBX contains opaque, incomplete or undecodable data')
        if not fingerprint['rawx_epochs'] and source.utc_day is None:
            if file_fingerprint(source.path)!=dict(bytes=fingerprint['bytes'],sha256=fingerprint['sha256']):
                raise ValueError('Source changed during conversion')
            report.update(status='SKIPPED',skip_reason='no_rawx_undated_control_only',outputs={})
            return
        if settings.obs and not fingerprint['rawx_signals']:
            raise ValueError('UBX has no usable RAWX measurements for OBS')
        if not engine:
            try:
                probe(settings,engine)
            except BaseException:
                engine.clear()
                raise
        report['args']=command(settings,source,engine['options'])
        if file_fingerprint(settings.executable)!=dict(bytes=engine['bytes'],sha256=engine['sha256']):
            raise ValueError('convbin executable changed after compatibility check')
        report['process']=invoke(report['args'],work,settings.timeout_s)
        if report['process']['timed_out']:
            raise ValueError('convbin timed out after '+str(settings.timeout_s)+' seconds')
        if report['process']['returncode']!=0:
            raise ValueError('convbin returned a nonzero exit code')
        if file_fingerprint(source.path)!=dict(bytes=fingerprint['bytes'],sha256=fingerprint['sha256']):
            raise ValueError('Source changed during conversion')
        if file_fingerprint(settings.executable)!=dict(bytes=engine['bytes'],sha256=engine['sha256']):
            raise ValueError('convbin executable changed during conversion')
        checks=report['validation']
        warnings=report['warnings']
        for key in outputs:
            if (work/('output.'+key)).is_symlink():
                raise ValueError('convbin output must be a regular file')
        if settings.obs:
            obs=checks['obs']=observations(work/'output.obs',settings.interval_s)
            requested=set(settings.masks)
            emitted={code[0]+code[2:] for code in obs['signals']}
            # Some RINEX 3.02 builds label B1I as 1I in the output.
            emitted={('C2I' if code=='C1I' else code) for code in emitted}
            if (any(code[1] not in settings.observations for code in obs['signals'])
                    or emitted-{mask[0]+mask[2:] for mask in requested}):
                raise ValueError('OBS contains types or signals outside the requested profile')
            expected=set()
            unknown=[]
            available_groups=set()
            for key in fingerprint['rawx_signals']:
                g,s=map(int,key.split(':'))
                if GNSS.get(g) not in settings.systems:
                    continue
                known=SIGNALS.get((g,s))
                if known:
                    system,code,slot=known
                    alternatives={system+'L'+code,system+'L'+COMBINED.get((system,code),code)}
                    selected=alternatives & requested
                    if selected:
                        available_groups.add(GROUPS[slot-1])
                        expected.add(tuple(sorted(mask[0]+mask[2:] for mask in selected)))
                else:
                    unknown.append(key)
            missing=['/'.join(codes) for codes in sorted(expected) if not emitted.intersection(codes)]
            if missing:
                warnings.append('Requested source signals missing in OBS: '+', '.join(missing))
            if unknown:
                warnings.append('Source RAWX signals outside the supported RINEX 3.02 profile: '+', '.join(unknown))
            absent=sorted(set(settings.groups)-available_groups)
            if absent:
                warnings.append('No usable source measurements for requested groups: '+', '.join(absent))
            missing_types=sorted(set(settings.observations)-{code[1] for code in obs['signals']})
            if missing_types:
                warnings.append('Requested observation types absent: '+', '.join(missing_types))
            if obs['gaps'] or obs['nonmonotonic_epochs'] or obs['epochs']!=fingerprint['rawx_epochs']:
                warnings.append('OBS epoch continuity/count differs from nominal RAWX stream. Original segments preserved.')
        missing_nav=False
        if settings.nav and settings.obs and settings.allow_obs_without_nav:
            try:
                (work/'output.nav').stat()
            except FileNotFoundError:
                missing_nav=True
        if missing_nav:
            report['status']=OBS_ONLY
            report['requested_outputs']=report['outputs'].copy()
            report['missing_outputs']=['nav']
            warnings.append('NAV missing: validated OBS published without navigation; conversion is incomplete')
            report['outputs'].pop('nav')
            outputs.pop('nav')
        elif settings.nav:
            try:
                nav=checks['nav']=navigation(work/'output.nav')
                missing=(set(fingerprint['sfrbx_systems']) & set(settings.systems))-set(nav['records'])
                aux=[p.name for p in work.iterdir()
                     if p.name not in {'input.ubx','output.obs','output.nav'}
                     and p.is_file() and p.stat().st_size]
                if missing or aux or set(nav['records'])-set(settings.systems):
                    raise ValueError('Mixed NAV coverage uncertain. Missing systems with SFRBX: '
                                     +','.join(sorted(missing))+'; auxiliary files: '+','.join(sorted(aux)))
            except (OSError,ValueError) as error:
                report['errors'].append(str(error))
                report['status']='INCOMPLETE_NAVIGATION'
                return
        report['output_fingerprints']={key:file_fingerprint(work/('output.'+key)) for key in outputs}
        try:
            commit(work,receipt,identity(settings,report,engine),report)
        except FileExistsError as error:
            report['status']='CONFLICT'
            report['errors'].append(str(error))


def boundaries(segments, interval):
    results=[]
    segments=[segment for segment in segments if segment['status']!='SKIPPED']
    for previous,current in zip(segments,segments[1:]):
        a,b=(item['validation'].get('obs') for item in (previous,current))
        if not a or not b or not a['time_system'] or a['time_system']!=b['time_system']:
            continue
        seconds=(datetime.fromisoformat(b['first_epoch'])-datetime.fromisoformat(a['last_epoch'])).total_seconds()
        if abs(seconds-interval)>interval*.1:
            results.append(dict(before=previous['source']['path'],after=current['source']['path'],
                                seconds=seconds,time_system=a['time_system']))
    return results
