#!/usr/bin/env python
#
# Script to build and install packages into the Steam runtime

from __future__ import print_function
import calendar
import errno
import os
import re
import sys
import gzip
import hashlib
import shutil
import subprocess
import tarfile
import time
from contextlib import closing, contextmanager
from debian import deb822
from debian.debian_support import Version
import argparse

try:
	from io import BytesIO
except ImportError:
	from cStringIO import StringIO as BytesIO

try:
	from urllib.request import (urlopen, urlretrieve)
except ImportError:
	from urllib import (urlopen, urlretrieve)

destdir="newpkg"
arches=["amd64", "i386"]

# The top level directory
top = sys.path[0]

ONE_MEGABYTE = 1024 * 1024
SPLIT_MEGABYTES = 50
MIN_PARTS = 3


def str2bool (b):
	return b.lower() in ("yes", "true", "t", "1")


def check_path_traversal(s):
	if '..' in s or s.startswith('/'):
		raise ValueError('Path traversal detected in %r' % s)


class AptSource:
	def __init__(
		self,
		kind,
		url,
		suite,
		components=('main',),
		trusted=False
	):
		self.kind = kind
		self.url = url
		self.suite = suite
		self.components = components
		self.trusted = trusted

	def __str__(self):
		if self.trusted:
			maybe_options = ' [trusted=yes]'
		else:
			maybe_options = ''

		return '%s%s %s %s %s' % (
			self.kind,
			maybe_options,
			self.url,
			self.suite,
			' '.join(self.components),
		)

	@property
	def release_url(self):
		return '%s/dists/%s/Release' % (self.url, self.suite)

	@property
	def sources_urls(self):
		if self.kind != 'deb-src':
			return []

		return [
			"%s/dists/%s/%s/source/Sources.gz" % (
				self.url, self.suite, component)
			for component in self.components
		]

	def get_packages_urls(self, arch, dbgsym=False):
		if self.kind != 'deb':
			return []

		if dbgsym:
			maybe_debug = 'debug/'
		else:
			maybe_debug = ''

		return [
			"%s/dists/%s/%s/%sbinary-%s/Packages" % (
				self.url, self.suite, component,
				maybe_debug, arch)
			for component in self.components
		]


def parse_args():
	parser = argparse.ArgumentParser()
	parser.add_argument(
		"--templates",
		help="specify template files to include in runtime",
		default=os.path.join(top, "templates"))
	parser.add_argument("-r", "--runtime", help="specify runtime path", default=os.path.join(top,"runtime"))
	parser.add_argument("--suite", help="specify apt suite", default='scout')
	parser.add_argument("-b", "--beta", help="build beta runtime", dest='suite', action="store_const", const='scout_beta')
	parser.add_argument("-d", "--debug", help="build debug runtime", action="store_true")
	parser.add_argument("--source", help="include sources", action="store_true")
	parser.add_argument("--symbols", help="include debugging symbols", action="store_true")
	parser.add_argument(
		"--repo", help="main apt repository URL",
		default="http://repo.steampowered.com/steamrt",
	)
	parser.add_argument(
		"--extra-apt-source", dest='extra_apt_sources',
		default=[], action='append',
		help=(
			"additional apt sources in the form "
			"'deb http://URL SUITE COMPONENT [COMPONENT...]' "
			"(may be repeated)"
		),
	)
	parser.add_argument("-v", "--verbose", help="verbose", action="store_true")
	parser.add_argument("--official", help="mark this as an official runtime", action="store_true")
	parser.add_argument("--set-name", help="set name for this runtime", default=None)
	parser.add_argument("--set-version", help="set version number for this runtime", default=None)
	parser.add_argument("--debug-url", help="set URL for debug/source version", default=None)
	parser.add_argument("--archive", help="pack Steam Runtime into a tarball", default=None)
	parser.add_argument(
		"--compression", help="set compression [xz|gx|bz2|none]",
		choices=('xz', 'gz', 'bz2', 'none'),
		default='xz')
	parser.add_argument(
		'--strict', action="store_true",
		help='Exit unsuccessfully when something seems wrong')
	parser.add_argument(
		'--split', default=None,
		help='Also generate an archive split into 50M parts')

	args = parser.parse_args()

	if args.split is not None and args.archive is None:
		parser.error('--split requires --archive')

	if not os.path.isdir(args.templates):
		parser.error(
			'Argument to --templates, %r, must be a directory'
			% args.templates)

	# os.path.exists is false for dangling symlinks, so check for both
	if os.path.exists(args.runtime) or os.path.islink(args.runtime):
		parser.error(
			'Argument to --runtime, %r, must not already exist'
			% args.runtime)

	return args


def download_file(file_url, file_path):
	try:
		if os.path.getsize(file_path) > 0:
			return False
	except OSError:
		pass

	urlretrieve(file_url, file_path)
	return True


class SourcePackage:
	def __init__(self, apt_source, stanza):
		self.apt_source = apt_source
		self.stanza = stanza


def install_sources(apt_sources, sourcelist):
	# Load the Sources files so we can find the location of each source package
	source_packages = []

	for apt_source in apt_sources:
		for url in apt_source.sources_urls:
			print("Downloading sources from %s" % url)
			sz = urlopen(url)
			url_file_handle=BytesIO(sz.read())
			sources = gzip.GzipFile(fileobj=url_file_handle)
			for stanza in deb822.Sources.iter_paragraphs(sources):
				source_packages.append(
					SourcePackage(apt_source, stanza))

	skipped = 0
	unpacked = []
	manifest_lines = set()

	# Walk through the Sources file and process any requested packages.
	# If a particular source package name appears more than once (for
	# example in scout and also in an overlay suite), we err on the side
	# of completeness and download all of them.
	for sp in source_packages:
		p = sp.stanza['package']
		if p in sourcelist:
			if args.verbose:
				print("DOWNLOADING SOURCE: %s" % p)

			#
			# Create the destination directory if necessary
			#
			dir = os.path.join(top, destdir, "source", p)
			if not os.access(dir, os.W_OK):
				os.makedirs(dir)

			#
			# Download each file
			#
			for file in sp.stanza['files']:
				check_path_traversal(file['name'])
				file_path = os.path.join(dir, file['name'])
				file_url = "%s/%s/%s" % (
					sp.apt_source.url,
					sp.stanza['directory'],
					file['name']
				)
				if not download_file(file_url, file_path):
					if args.verbose:
						print("Skipping download of existing deb source file(s): %s" % file_path)
					else:
						skipped += 1

			#
			# Unpack the source package into the runtime directory
			#
			dest_dir=os.path.join(args.runtime,"source",p)
			if os.access(dest_dir, os.W_OK):
				shutil.rmtree(dest_dir)
			os.makedirs(dest_dir)
			dsc_file = os.path.join(
				dir,
				sp.stanza['files'][0]['name']
			)
			ver = sp.stanza['files'][0]['name'].split('-')[0]
			process = subprocess.Popen(["dpkg-source", "-x", "--no-copy", dsc_file, os.path.join(dest_dir,ver)], stdout=subprocess.PIPE, universal_newlines=True)
			for line in iter(process.stdout.readline, ""):
				if args.verbose or re.match(r'dpkg-source: warning: ',line):
					print(line, end='')

			unpacked.append((p, sp.stanza['Version'], sp.stanza))
			manifest_lines.add(
				'%s\t%s\t%s\n' % (
					p, sp.stanza['Version'],
					sp.stanza['files'][0]['name']))

	# sources.txt: Tab-separated table of source packages, their
	# versions, and the corresponding .dsc file.
	with open(os.path.join(args.runtime, 'source', 'sources.txt'), 'w') as writer:
		writer.write('#Source\t#Version\t#dsc\n')

		for line in sorted(manifest_lines):
			writer.write(line)

	# sources.deb822.gz: The full Sources stanza for each included source
	# package, suitable for later analysis.
	with open(
		os.path.join(args.runtime, 'source', 'sources.deb822.gz'), 'wb'
	) as gz_writer:
		with gzip.GzipFile(
			filename='', fileobj=gz_writer, mtime=0
		) as stanza_writer:
			done_one = False

			for source in sorted(unpacked):
				if done_one:
					stanza_writer.write(b'\n')

				source[2].dump(stanza_writer)
				done_one = True

	if skipped > 0:
		print("Skipped downloading %i deb source file(s) that were already present." % skipped)


class Binary:
	def __init__(self, apt_source, stanza):
		self.apt_source = apt_source
		self.stanza = stanza
		self.name = stanza['Package']
		self.arch = stanza['Architecture']
		self.version = stanza['Version']
		source = stanza.get('Source', self.name)

		if ' (' in source:
			self.source, tmp = source.split(' (', 1)
			self.source_version = tmp.rstrip(')')
		else:
			self.source = source
			self.source_version = self.version


def list_binaries(apt_sources, dbgsym=False):
	by_arch = {}

	if dbgsym:
		description = 'debug symbols'
	else:
		description = 'binaries'

	for arch in arches:
		by_name = {}

		# Load the Packages files so we can find the location of each
		# binary package
		for apt_source in apt_sources:
			for url in apt_source.get_packages_urls(
				arch,
				dbgsym=dbgsym,
			):
				print("Downloading %s %s from %s" % (
					arch, description, url))

				try:
					url_file_handle = BytesIO(urlopen(url).read())
				except Exception as e:
					if dbgsym:
						print(e)
						continue
					else:
						raise

				for stanza in deb822.Packages.iter_paragraphs(
					url_file_handle
				):
					p = stanza['Package']
					binary = Binary(apt_source, stanza)
					by_name.setdefault(p, []).append(
						binary)

		by_arch[arch] = by_name

	return by_arch


def install_binaries(binaries_by_arch, binarylist, manifest):
	skipped = 0

	for arch, arch_binaries in sorted(binaries_by_arch.items()):
		installset = binarylist.copy()

		#
		# Create the destination directory if necessary
		#
		dir = os.path.join(top,destdir,"binary" if not args.debug else "debug", arch)
		if not os.access(dir, os.W_OK):
			os.makedirs(dir)

		for p, binaries in sorted(arch_binaries.items()):
			if p in installset:
				if args.verbose:
					print("DOWNLOADING BINARY: %s" % p)

				newest = max(
					binaries,
					key=lambda b:
						Version(b.stanza['Version']))
				manifest[(p, arch)] = newest

				#
				# Download the package and install it
				#
				check_path_traversal(newest.stanza['Filename'])
				file_url = "%s/%s" % (
					newest.apt_source.url,
					newest.stanza['Filename'],
				)
				dest_deb = os.path.join(
					dir,
					os.path.basename(newest.stanza['Filename']),
				)
				if not download_file(file_url, dest_deb):
					if args.verbose:
						print("Skipping download of existing deb: %s" % dest_deb)
					else:
						skipped += 1
				install_deb(
					os.path.splitext(
						os.path.basename(
							newest.stanza['Filename']
						)
					)[0],
					dest_deb,
					os.path.join(args.runtime, arch)
				)
				installset.remove(p)

		for p in installset:
			#
			# There was a binary package in the list to be installed that is not in the repo
			#
			e = "ERROR: Package %s not found in Packages files\n" % p
			sys.stderr.write(e)

		if installset and args.strict:
			raise SystemExit('Not all binary packages were found')

	if skipped > 0:
		print("Skipped downloading %i file(s) that were already present." % skipped)


def install_deb (basename, deb, dest_dir):
	check_path_traversal(basename)
	installtag_dir=os.path.join(dest_dir, "installed")
	if not os.access(installtag_dir, os.W_OK):
		os.makedirs(installtag_dir)

	#
	# Write the tag file and checksum to the 'installed' subdirectory
	#
	with open(os.path.join(installtag_dir, basename), "w") as f:
		subprocess.check_call(['dpkg-deb', '-c', deb], stdout=f)
	with open(os.path.join(installtag_dir, basename + ".md5"), "w") as f:
		os.chdir(os.path.dirname(deb))
		subprocess.check_call(['md5sum', os.path.basename(deb)], stdout=f)

	#
	# Unpack the package into the dest_dir
	#
	os.chdir(top)
	subprocess.check_call(['dpkg-deb', '-x', deb, dest_dir])


def install_symbols(dbgsym_by_arch, binarylist, manifest):
	skipped = 0
	for arch, arch_binaries in sorted(dbgsym_by_arch.items()):

		#
		# Create the destination directory if necessary
		#
		dir = os.path.join(top,destdir, "symbols", arch)
		if not os.access(dir, os.W_OK):
			os.makedirs(dir)

		for p, binaries in sorted(arch_binaries.items()):
			if not p.endswith('-dbgsym'):
				# not a detached debug symbol package
				continue

			# If p is libfoo2-dbgsym, then parent_name is libfoo2.
			parent_name = p[:-len('-dbgsym')]
			parent = manifest.get((parent_name, arch))

			# We only download detached debug symbols for
			# packages that we already installed for the
			# corresponding architecture
			if parent is not None:
				# Find a matching version if we can
				tried = []

				for b in binaries:
					if b.version == parent.version:
						dbgsym = b
						break
					else:
						tried.append(b.version)
				else:
					# There's no point in installing
					# detached debug symbols if they don't
					# match
					tried.sort()
					sys.stderr.write(
						'WARNING: Debug symbol package '
						'%s not found at version %s '
						'(available: %s)\n' % (
							p,
							parent.version,
							', '.join(tried),
						)
					)
					continue

				manifest[(p, arch)] = dbgsym

				if args.verbose:
					print("DOWNLOADING SYMBOLS: %s" % p)
				#
				# Download the package and install it
				#
				check_path_traversal(dbgsym.stanza['Filename'])
				file_url = "%s/%s" % (
					dbgsym.apt_source.url,
					dbgsym.stanza['Filename'],
				)
				dest_deb = os.path.join(
					dir,
					os.path.basename(
						dbgsym.stanza['Filename'])
				)
				if not download_file(file_url, dest_deb):
					if args.verbose:
						print("Skipping download of existing symbol deb: %s", dest_deb)
					else:
						skipped += 1
				install_deb(
					os.path.splitext(
						os.path.basename(
							dbgsym.stanza['Filename'])
					)[0],
					dest_deb,
					os.path.join(args.runtime, arch)
				)

	if skipped > 0:
		print("Skipped downloading %i symbol deb(s) that were already present." % skipped)


# Walks through the files in the runtime directory and converts any absolute symlinks
# to their relative equivalent
#
def fix_symlinks ():
	for arch in arches:
		for dir, subdirs, files in os.walk(os.path.join(args.runtime,arch)):
			for name in files:
				filepath=os.path.join(dir,name)
				if os.path.islink(filepath):
					target = os.readlink(filepath)
					if os.path.isabs(target):
						#
						# compute the target of the symlink based on the 'root' of the architecture's runtime
						#
						target2 = os.path.join(args.runtime,arch,target[1:])

						#
						# Set the new relative target path
						#
						os.unlink(filepath)
						os.symlink(os.path.relpath(target2,dir), filepath)


# Creates the usr/lib/debug/.build-id/xx/xxxxxxxxx.debug symlink tree for all the debug
# symbols
#
def fix_debuglinks ():
	for arch in arches:
		for dir, subdirs, files in os.walk(os.path.join(args.runtime,arch,"usr/lib/debug")):
			if ".build-id" in subdirs:
				subdirs.remove(".build-id")		# don't recurse into .build-id directory we are creating

			for file in files:

				#
				# scrape the output of readelf to find the buildid for this binary
				#
				p = subprocess.Popen(["readelf", '-n', os.path.join(dir,file)], stdout=subprocess.PIPE, universal_newlines=True)
				for line in iter(p.stdout.readline, ""):
					m = re.search(r'Build ID: (\w{2})(\w+)',line)
					if m:
						check_path_traversal(m.group(1))
						check_path_traversal(m.group(2))
						linkdir = os.path.join(args.runtime,arch,"usr/lib/debug/.build-id",m.group(1))
						if not os.access(linkdir, os.W_OK):
							os.makedirs(linkdir)
						link = os.path.join(linkdir,m.group(2))
						if args.verbose:
							print("SYMLINKING symbol file %s to %s" % (link, os.path.relpath(os.path.join(dir,file),linkdir)))
						if os.path.lexists(link):
							os.unlink(link)
						os.symlink(os.path.relpath(os.path.join(dir,file), linkdir),link)


def write_manifests(manifest):
	done = set()

	# manifest.deb822: The full Packages stanza for each installed package,
	# suitable for later analysis.
	with open(os.path.join(args.runtime, 'manifest.deb822.gz'), 'wb') as out:
		with gzip.GzipFile(filename='', fileobj=out, mtime=0) as writer:
			for key, binary in sorted(manifest.items()):
				if key in done:
					continue

				if done:
					writer.write(b'\n')

				binary.stanza.dump(writer)
				done.add(key)

	# manifest.txt: A summary of installed binary packages, as
	# a table of tab-separated values.
	lines = set()

	for binary in manifest.values():
		lines.add('%s:%s\t%s\t%s\t%s\n' % (binary.name, binary.arch, binary.version, binary.stanza.get('Source', binary.name), binary.stanza.get('Installed-Size', '')))

	with open(os.path.join(args.runtime, 'manifest.txt'), 'w') as writer:
		writer.write('#Package[:Architecture]\t#Version\t#Source\t#Installed-Size\n')

		for line in sorted(lines):
			writer.write(line)

	# built-using.txt: A summary of source packages that were embedded in
	# installed binary packages, as a table of tab-separated values.
	lines = set()

	for binary in manifest.values():
		built_using = binary.stanza.get('Built-Using', '')

		if not built_using:
			continue

		relations = built_using.split(',')

		for relation in relations:
			relation = relation.replace(' ', '')
			assert '(=' in relation, relation
			p, v = relation.split('(=', 1)
			assert v[-1] == ')', relation
			v = v[:-1]
			lines.add('%s\t%s\t%s\n' % (binary.name, p, v))

	with open(os.path.join(args.runtime, 'built-using.txt'), 'w') as writer:
		writer.write('#Built-Binary\t#Built-Using-Source\t#Built-Using-Version\n')

		for line in sorted(lines):
			writer.write(line)


@contextmanager
def waiting(popen):
	"""
	Context manager to wait for a subprocess.Popen object to finish,
	similar to contextlib.closing().

	Popen objects are context managers themselves, but only in
	Python 3.2 or later.
	"""
	try:
		yield popen
	finally:
		popen.stdin.close()
		popen.wait()


def normalize_tar_entry(entry):
	# type: (TarInfo) -> TarInfo
	if args.verbose:
		print(entry.name)

	entry.uid = 65534
	entry.gid = 65534

	if entry.mtime > reference_timestamp:
		entry.mtime = reference_timestamp

	entry.uname = 'nobody'
	entry.gname = 'nogroup'

	return entry


# Create files u=rwX,go=rX by default
os.umask(0o022)

args = parse_args()
if args.verbose:
	for property, value in sorted(vars(args).items()):
		print("\t", property, ": ", value)

if args.debug:
	component = 'debug'
else:
	component = 'main'

apt_sources = [
	AptSource('deb', args.repo, args.suite, (component,)),
	AptSource('deb-src', args.repo, args.suite, (component,)),
]

for line in args.extra_apt_sources:
	trusted=False
	tokens = line.split()

	if len(tokens) < 4:
		raise ValueError(
			'--extra-apt-source argument must be in the form '
			'"deb http://URL SUITE COMPONENT [COMPONENT...]"')

	if tokens[0] not in ('deb', 'deb-src', 'both'):
		raise ValueError(
			'--extra-apt-source argument must start with '
			'"deb ", "deb-src " or "both "')

	if tokens[1] == '[trusted=yes]':
		trusted=True
		tokens = [tokens[0]] + tokens[2:]
	elif tokens[1].startswith('['):
		raise ValueError(
			'--extra-apt-source does not support [opt=value] '
			'syntax, except for [trusted=yes]')

	if tokens[0] == 'both':
		apt_sources.append(
			AptSource(
				'deb', tokens[1], tokens[2], tokens[3:],
				trusted=trusted,
			)
		)
		apt_sources.append(
			AptSource(
				'deb-src', tokens[1], tokens[2], tokens[3:],
				trusted=trusted,
			)
		)
	else:
		apt_sources.append(
			AptSource(
				tokens[0], tokens[1], tokens[2], tokens[3:],
				trusted=trusted,
			)
		)

timestamps = {}

for source in apt_sources:
	with closing(urlopen(source.release_url)) as release_file:
		release_info = deb822.Deb822(release_file)
		timestamps[source] = calendar.timegm(time.strptime(
			release_info['date'],
			'%a, %d %b %Y %H:%M:%S %Z',
		))

if 'SOURCE_DATE_EPOCH' in os.environ:
	reference_timestamp = int(os.environ['SOURCE_DATE_EPOCH'])
else:
	reference_timestamp = max(timestamps.values())

if args.set_name is not None:
	name = args.set_name
else:
	name = 'steam-runtime'

	if not args.official:
		name = 'unofficial-' + name

	if apt_sources[0].suite == 'scout_beta':
		name = '%s-beta' % name
	elif apt_sources[0].suite != 'scout':
		name = '%s-%s' % (name, apt_sources[0].suite)

	if args.symbols:
		name += '-sym'

	if args.source:
		name += '-src'

	if args.debug:
		name += '-debug'
	else:
		name += '-release'

if args.set_version is not None:
	version = args.set_version
else:
	version = time.strftime('snapshot-%Y%m%d-%H%M%SZ', time.gmtime())

name_version = '%s_%s' % (name, version)

# Populate runtime from template
shutil.copytree(args.templates, args.runtime, symlinks=True)

with open(os.path.join(args.runtime, 'version.txt'), 'w') as writer:
	writer.write('%s\n' % name_version)

if args.debug_url:
	# Note where people can get the debug version of this runtime
	with open(
		os.path.join(args.templates, 'README.txt')
	) as reader:
		with open(
			os.path.join(args.runtime, 'README.txt.new'), 'w'
		) as writer:
			for line in reader:
				line = re.sub(
					r'https?://media\.steampowered\.com/client/runtime/.*$',
					args.debug_url, line)
				writer.write(line)

	os.rename(
		os.path.join(args.runtime, 'README.txt.new'),
		os.path.join(args.runtime, 'README.txt'))

# Process packages.txt to get the list of source and binary packages
source_pkgs = set()
binary_pkgs = set()

print("Creating Steam Runtime in %s" % args.runtime)

with open("packages.txt") as f:
	for line in f:
		if line[0] != '#':
			toks = line.split()
			if len(toks) > 1:
				source_pkgs.add(toks[0])
				binary_pkgs.update(toks[1:])

# remove development packages for end-user runtime
if not args.debug:
	binary_pkgs -= {x for x in binary_pkgs if re.search('-dbg$|-dev$|-multidev$',x)}

if args.source:
	install_sources(apt_sources, source_pkgs)

# {('libfoo2', 'amd64'): Binary for libfoo2_1.2-3_amd64}
manifest = {}

binaries_by_arch = list_binaries(apt_sources)
install_binaries(binaries_by_arch, binary_pkgs, manifest)

if args.symbols:
	dbgsym_by_arch = list_binaries(apt_sources, dbgsym=True)
	install_symbols(dbgsym_by_arch, binary_pkgs, manifest)
	fix_debuglinks()

fix_symlinks()

write_manifests(manifest)

print("Normalizing permissions...")
subprocess.check_call([
	'chmod', '--changes', 'u=rwX,go=rX', '--', args.runtime,
])

if args.archive is not None:
	if args.archive.endswith('/'):
		try:
			os.makedirs(args.archive, 0o755)
		except OSError as e:
			if e.errno != errno.EEXIST:
				raise

	if args.compression == 'none':
		ext = '.tar'
	else:
		ext = '.tar.' + args.compression

	if os.path.isdir(args.archive):
		archive = os.path.join(args.archive, name_version + ext)
		archive_dir = args.archive
	else:
		archive = args.archive
		archive_dir = None

	print("Creating archive %s..." % archive)

	with open(archive, 'wb') as archive_writer, waiting(subprocess.Popen(
		['xz', '-v'],
		stdin=subprocess.PIPE,
		stdout=archive_writer,
	)) as xz, closing(tarfile.open(
		archive,
		mode='w|',
		format=tarfile.GNU_FORMAT,
		fileobj=xz.stdin,
	)) as archiver:
		members = []

		for dir_path, dirs, files in os.walk(
			args.runtime,
			topdown=True,
			followlinks=False,
		):
			rel_dir_path = os.path.relpath(
				dir_path, args.runtime)

			if not rel_dir_path.startswith('./'):
				rel_dir_path = './' + rel_dir_path

			for member in dirs:
				members.append(
					os.path.join(rel_dir_path, member))

			for member in files:
				members.append(
					os.path.join(rel_dir_path, member))

		for member in sorted(members):
			archiver.add(
				os.path.join(args.runtime, member),
				arcname=os.path.normpath(
					os.path.join(
						'steam-runtime',
						member,
					)),
				recursive=False,
				filter=normalize_tar_entry,
			)

	print("Creating archive checksum %s.checksum..." % archive)
	archive_md5 = hashlib.md5()

	with open(archive, 'rb') as archive_reader:
		while True:
			blob = archive_reader.read(ONE_MEGABYTE)

			if not blob:
				break

			archive_md5.update(blob)

	with open(archive + '.checksum', 'w') as writer:
		writer.write('%s  %s\n' % (
			archive_md5.hexdigest(), os.path.basename(archive)
		))

	if archive_dir is not None:
		print("Copying manifest files to %s..." % archive_dir)

		with open(
			os.path.join(
				archive_dir,
				name_version + '.sources.list'),
			'w'
		) as writer:
			for apt_source in apt_sources:
				writer.write(
					time.strftime(
						'# as of %Y-%m-%d %H:%M:%S\n',
						time.gmtime(
							timestamps[apt_source]
						)
					)
				)
				writer.write('%s\n' % apt_source)

		shutil.copy(
			os.path.join(args.runtime, 'manifest.txt'),
			os.path.join(
				archive_dir, name_version + '.manifest.txt'),
		)
		shutil.copy(
			os.path.join(args.runtime, 'built-using.txt'),
			os.path.join(
				archive_dir, name_version + '.built-using.txt'),
		)
		shutil.copy(
			os.path.join(args.runtime, 'manifest.deb822.gz'),
			os.path.join(
				archive_dir,
				name_version + '.manifest.deb822.gz'),
		)
		if args.source:
			shutil.copy(
				os.path.join(
					args.runtime,
					'source',
					'sources.txt'),
				os.path.join(
					archive_dir,
					name_version + '.sources.txt'),
			)
			shutil.copy(
				os.path.join(
					args.runtime,
					'source',
					'sources.deb822.gz'),
				os.path.join(
					archive_dir,
					name_version + '.sources.deb822.gz'),
			)

	if archive_dir is not None and version != 'latest':
		print("Creating symlink %s_latest%s..." % (name, ext))
		symlink = os.path.join(archive_dir, name + '_latest' + ext)

		try:
			os.remove(symlink)
		except OSError as e:
			if e.errno != errno.ENOENT:
				raise

		try:
			os.remove(symlink + '.checksum')
		except OSError as e:
			if e.errno != errno.ENOENT:
				raise

		os.symlink(os.path.basename(archive), symlink)
		os.symlink(
			os.path.basename(archive) + '.checksum',
			symlink + '.checksum')

	if args.split:
		with open(archive, 'rb') as archive_reader:
			part = 0
			position = 0
			part_writer = open(args.split + ext + '.part0', 'wb')

			while True:
				blob = archive_reader.read(ONE_MEGABYTE)

				if not blob:
					break

				if position >= SPLIT_MEGABYTES:
					part += 1
					position -= SPLIT_MEGABYTES
					part_writer.close()
					part_writer = open(
						'%s%s.part%d' % (
							args.split, ext, part
						),
						'wb')

				part_writer.write(blob)
				position += 1

			while part < MIN_PARTS - 1:
				part += 1
				part_writer.close()
				part_writer = open(
					'%s%s.part%d' % (
						args.split, ext, part
					),
					'wb')

			part_writer.close()

		with open(args.split + '.checksum', 'w') as writer:
			writer.write('%s  %s%s\n' % (
				archive_md5.hexdigest(),
				os.path.basename(args.split),
				ext,
			))

# vi: set noexpandtab:
