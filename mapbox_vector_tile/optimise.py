from mapbox_vector_tile.Mapbox.vector_tile_pb2 import tile
from collections import namedtuple


class StringTableOptimiser(object):
    """
    Optimises the order of keys and values in the MVT layer string table.

    Counts the number of times an entry in the MVT string table (both keys
    and values) is used. Then reorders the string table to have the most
    commonly used entries first and updates the features to use the
    replacement locations in the table. This can save several percent in a
    tile with large numbers of features.
    """

    def __init__(self):
        self.key_counts = {}
        self.val_counts = {}

    def add_tags(self, feature_tags):
        itr = iter(feature_tags)
        for k, v in zip(itr, itr):
            self.key_counts[k] = self.key_counts.get(k, 0) + 1
            self.val_counts[v] = self.val_counts.get(v, 0) + 1

    def _update_table(self, counts, table):
        # sort string table by usage, so most commonly-used values are
        # assigned the smallest indices. since indices are encoded as
        # varints, this should make best use of the space.
        sort = list(sorted(
            ((c, k) for k, c in counts.iteritems()),
            reverse=True))

        # construct the re-ordered string table
        new_table = []
        for _, x in sort:
            new_table.append(table[x])
        assert len(new_table) == len(table)

        # delete table in place and replace with the new table
        del table[:]
        table.extend(new_table)

        # construct a lookup table from the old to the new indices.
        new_indexes = {}
        for i, (c, k) in enumerate(sort):
            new_indexes[k] = i

        return new_indexes

    def update_string_table(self, layer):
        new_key = self._update_table(self.key_counts, layer.keys)
        new_val = self._update_table(self.val_counts, layer.values)

        for feature in layer.features:
            for i in xrange(0, len(feature.tags), 2):
                feature.tags[i] = new_key[feature.tags[i]]
                feature.tags[i+1] = new_val[feature.tags[i+1]]


# return the signed integer corresponding to the "zig-zag" encoded unsigned
# input integer. this encoding is used for MVT geometry deltas.
def unzigzag(n):
    return (n >> 1) ^ (-(n & 1))


# return the "zig-zag" encoded unsigned integer corresponding to the signed
# input integer. this encoding is used for MVT geometry deltas.
def zigzag(n):
    return (n << 1) ^ (n >> 31)


# we assume that every linestring consists of a single MoveTo command followed
# by some number of LineTo commands, and we encode this as a Line object.
#
# normally, MVT linestrings are encoded relative to the preceding linestring
# (if any) in the geometry. however that's awkward for reodering, so we
# construct an absolute MoveTo for each Line. we also derive a corresponding
# EndsAt location, which isn't used in the encoding, but simplifies analysis.
MoveTo = namedtuple('MoveTo', 'x y')
EndsAt = namedtuple('EndsAt', 'x y')
Line = namedtuple('Line', 'moveto endsat cmds')


def _decode_lines(geom):
    """
    Decode a linear MVT geometry into a list of Lines.

    Each individual linestring in the MVT is extracted to a separate entry in
    the list of lines.
    """

    lines = []
    current_line = []
    current_moveto = None

    # to keep track of the position. we'll adapt the move-to commands to all
    # be relative to 0,0 at the beginning of each linestring.
    x = 0
    y = 0

    end = len(geom)
    i = 0
    while i < end:
        header = geom[i]
        cmd = header & 7
        run_length = header // 8

        if cmd == 1:  # move to
            # flush previous line.
            if current_moveto:
                lines.append(Line(current_moveto, EndsAt(x, y), current_line))
                current_line = []

            assert run_length == 1
            x += unzigzag(geom[i+1])
            y += unzigzag(geom[i+2])
            i += 3

            current_moveto = MoveTo(x, y)

        elif cmd == 2:  # line to
            assert current_moveto

            # we just copy this run, since it's encoding isn't going to change
            next_i = i + 1 + run_length * 2
            current_line.extend(geom[i:next_i])

            # but we still need to decode it to figure out where each move-to
            # command is in absolute space.
            for j in xrange(0, run_length):
                dx = unzigzag(geom[i + 1 + 2 * j])
                dy = unzigzag(geom[i + 2 + 2 * j])
                x += dx
                y += dy

            i = next_i

        else:
            raise ValueError('Unhandled command: %d' % cmd)

    if current_line:
        assert current_moveto
        lines.append(Line(current_moveto, EndsAt(x, y), current_line))

    return lines


def _reorder_lines(lines):
    """
    Reorder lines so that the distance from the end of one to the beginning of
    the next is minimised.
    """

    x = 0
    y = 0
    new_lines = []

    # treat the list of lines as a stack, off which we keep popping the best
    # one to add next.
    while lines:
        # looping over all the lines like this isn't terribly efficient, but
        # in local tests seems to handle a few thousand lines without a
        # problem.
        min_dist = None
        min_i = None
        for i, line in enumerate(lines):
            moveto, _, _ = line

            dist = abs(moveto.x - x) + abs(moveto.y - y)
            if min_dist is None or dist < min_dist:
                min_dist = dist
                min_i = i

        assert min_i is not None
        line = lines.pop(min_i)
        _, endsat, _ = line
        x = endsat.x
        y = endsat.y
        new_lines.append(line)

    return new_lines


def _rewrite_geometry(geom, new_lines):
    """
    Re-encode a list of Lines with absolute MoveTos as a continuous stream of
    MVT geometry commands, each relative to the last. Replace geom with that
    stream.
    """

    new_geom = []
    x = 0
    y = 0
    for line in new_lines:
        moveto, endsat, lineto_cmds = line

        dx = moveto.x - x
        dy = moveto.y - y
        x = endsat.x
        y = endsat.y

        new_geom.append(9)  # move to, run_length = 1
        new_geom.append(zigzag(dx))
        new_geom.append(zigzag(dy))
        new_geom.extend(lineto_cmds)

    # write the lines back out to geom
    del geom[:]
    geom.extend(new_geom)


def optimise_multilinestring(geom):
    # split the geometry into multiple lists, each starting with a move-to
    # command and consisting otherwise of line-to commands. (perhaps with
    # a close at the end? is that allowed for linestrings?)

    lines = _decode_lines(geom)

    # can't reorder anything unless it has multiple lines.
    if len(lines) > 1:
        lines = _reorder_lines(lines)
        _rewrite_geometry(geom, lines)


def optimise_tile(tile_bytes):
    """
    Decode a sequence of bytes as an MVT tile and reorder the string table of
    its layers and the order of its multilinestrings to save a few bytes.
    """

    t = tile()
    t.ParseFromString(tile_bytes)

    for layer in t.layers:
        sto = StringTableOptimiser()

        for feature in layer.features:
            # (multi)linestrings only
            if feature.type == 2:
                optimise_multilinestring(feature.geometry)

            sto.add_tags(feature.tags)

        sto.update_string_table(layer)

    return t.SerializeToString()


if __name__ == 'main':
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument('input_file', help='Input MVT file')
    parser.add_argument('--output-file', help='Output file, default is stdout')
    args = parser.parse_args()

    output_bytes = optimise_tile(open(args.input_file).read())

    if args.output_file:
        with open(args.output_file, 'w') as fh:
            fh.write(output_bytes)
    else:
        sys.stdout.write(output_bytes)
