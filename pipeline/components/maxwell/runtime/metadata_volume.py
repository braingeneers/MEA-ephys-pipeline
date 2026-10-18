"""Measure non-signal HDF5 payloads without guessing from compressed file bytes."""
import math

import h5py
import numpy as np

from ephys_modular.contracts import ContractError


class MetadataBudgetExceeded(ContractError):
    pass


class _Budget:
    def __init__(self, limit):
        if type(limit) is not int or limit <= 0:
            raise ContractError('Variable-metadata read budget must be a positive integer')
        self.limit, self.used, self.entries, self.variable_values = limit, 0, 0, 0
        self.reference_values = 0

    def consume(self, size):
        self.used += int(size)
        if self.used > self.limit:
            raise MetadataBudgetExceeded('Decoded variable metadata exceeds its explicit planning budget')

    def entry(self):
        self.entries += 1
        if self.entries > 100000:
            raise MetadataBudgetExceeded('Metadata attribute/dataset entry budget exhausted')


def _variable(dtype):
    dtype = np.dtype(dtype)
    if dtype.fields:
        return any(_variable(field[0]) for field in dtype.fields.values())
    if dtype.subdtype:
        return _variable(dtype.subdtype[0])
    return h5py.check_vlen_dtype(dtype) is not None


def _fixed_references(dtype):
    dtype = np.dtype(dtype)
    if dtype.fields:
        return sum(_fixed_references(dtype.fields[name][0]) for name in dtype.names)
    if dtype.subdtype:
        return math.prod(dtype.subdtype[1]) * _fixed_references(dtype.subdtype[0])
    return int(h5py.check_ref_dtype(dtype) is not None)


def _dynamic_bytes(value, dtype, budget, depth=0):
    if depth > 32:
        raise ContractError('Variable metadata nesting exceeds the planning limit')
    dtype = np.dtype(dtype)
    if dtype.fields:
        return sum(_dynamic_bytes(np.asarray(value)[name], dtype.fields[name][0], budget, depth + 1)
                   for name in dtype.names)
    if dtype.subdtype:
        return _dynamic_bytes(value, dtype.subdtype[0], budget, depth + 1)
    variable = h5py.check_vlen_dtype(dtype)
    if variable is None:
        if dtype.hasobject and h5py.check_ref_dtype(dtype) is None:
            raise ContractError('Unsupported object-valued metadata cannot be sized')
        budget.reference_values += np.asarray(value).size * _fixed_references(dtype)
        return 0
    total = 0
    for item in np.asarray(value, dtype=object).flat:
        budget.variable_values += 1
        if variable in (str, bytes):
            if not isinstance(item, (str, bytes, np.str_, np.bytes_)):
                raise ContractError('Malformed variable string metadata')
            size = len(item.encode('utf-8') if isinstance(item, str) else bytes(item)) + 1
            budget.consume(size)
            total += size
        else:
            array = np.asarray(item, dtype=np.dtype(variable))
            budget.consume(array.nbytes)
            total += array.nbytes + _dynamic_bytes(array, array.dtype, budget, depth + 1)
    return total


def _variable_dataset_bytes(dataset, budget):
    # Fixed parts of compounds are counted from the HDF5 type, without decoding
    # their data. A bounded batch limits the Python containers used for vlen reads.
    count = math.prod(dataset.shape)
    budget.consume(count * max(1, dataset.dtype.itemsize))
    if dataset.shape == ():
        return _dynamic_bytes(dataset[()], dataset.dtype, budget)
    row_elements = math.prod(dataset.shape[1:])
    rows = max(1, (1024 * 1024) // max(1, row_elements * dataset.dtype.itemsize))
    return sum(_dynamic_bytes(dataset[start:start + rows], dataset.dtype, budget)
               for start in range(0, dataset.shape[0], rows))


def measure_metadata(objects, raw_tokens, *, read_budget_bytes, include_objects=False):
    """Return payload facts plus read metrics, counting each physical object once.

    Fixed payloads use shape/HDF5 datatype size even when huge. Only vlen containers
    and decoded variable values consume the read budget. HDF5 may allocate one
    individual variable value before its decoded length can be checked; an
    allocation/read failure is terminal and never produces a completed plan.
    With include_objects, token keys are private live HDF5 identities for the
    caller's in-file graph accounting; remove that list before serializing facts.
    """
    budget = _Budget(read_budget_bytes)
    datasets = attributes = variable = names = attribute_count = dataset_count = 0
    object_facts = []
    fixed_references = 0
    for token, path, value in objects:
        before = (datasets,attributes,variable,names,budget.variable_values,budget.reference_values,fixed_references)
        names += len(path.encode('utf-8'))
        if isinstance(value, h5py.Dataset) and token not in raw_tokens:
            budget.entry()
            dataset_count += 1
            points = 0 if value.shape is None else math.prod(value.shape)
            payload = points * value.id.get_type().get_size()
            if points and _variable(value.dtype):
                dynamic = _variable_dataset_bytes(value, budget)
                variable += dynamic
                payload += dynamic
            elif points:
                fixed_references += points * _fixed_references(value.dtype)
            datasets += payload
        for name in value.attrs:
            budget.entry()
            attribute_count += 1
            names += len(name.encode('utf-8'))
            attribute = value.attrs.get_id(name)
            try:
                space, datatype = attribute.get_space(), attribute.get_type()
                try:
                    points = int(space.get_simple_extent_npoints())
                    payload = points * datatype.get_size()
                    if points and _variable(attribute.dtype):
                        budget.consume(points * max(1, attribute.dtype.itemsize))
                        dynamic = _dynamic_bytes(value.attrs[name], attribute.dtype, budget)
                        variable += dynamic
                        payload += dynamic
                    elif points:
                        fixed_references += points * _fixed_references(attribute.dtype)
                finally:
                    space.close()
                    datatype.close()
            finally:
                attribute.close()
            attributes += payload
        if include_objects:
            object_facts.append(dict(token=token,path=path,
                dataset_payload_bytes=datasets-before[0],attribute_payload_bytes=attributes-before[1],
                variable_payload_bytes=variable-before[2],name_bytes=names-before[3],
                variable_value_count=budget.variable_values-before[4],
                reference_value_count=budget.reference_values-before[5]+fixed_references-before[6]))
    facts = {'method': 'hdf5-type-shape-and-vlen-read-v1',
             'dataset_payload_bytes': datasets, 'attribute_payload_bytes': attributes,
             'variable_payload_bytes': variable, 'name_bytes': names,
             'total_payload_bytes': datasets + attributes + names,
             'dataset_count': dataset_count, 'attribute_count': attribute_count,
             'variable_value_count': budget.variable_values,
             'unique_objects': len(objects), 'complete': True}
    if include_objects:
        facts['objects'] = object_facts
        facts['reference_value_count'] = budget.reference_values + fixed_references
    return facts, {'variable_read_budget_bytes': budget.limit,
                   'variable_read_bytes': budget.used, 'metadata_entries': budget.entries}
