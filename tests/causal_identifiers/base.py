import pytest

from dowhy.graph import build_graph_from_str

from .example_graphs import TEST_GRAPH_SOLUTIONS


class IdentificationTestGraphSolution(object):
    def __init__(
        self,
        graph_str,
        observed_variables,
        biased_sets,
        minimal_adjustment_sets,
        maximal_adjustment_sets,
        direct_maximal_adjustment_sets=None,
    ):
        self.graph = build_graph_from_str(graph_str)
        self.action_nodes = ["X"]
        self.outcome_nodes = ["Y"]
        self.observed_nodes = observed_variables
        self.biased_sets = biased_sets
        self.minimal_adjustment_sets = minimal_adjustment_sets
        self.maximal_adjustment_sets = maximal_adjustment_sets
        self.direct_maximal_adjustment_sets = (
            maximal_adjustment_sets if direct_maximal_adjustment_sets is None else direct_maximal_adjustment_sets
        )


@pytest.fixture(params=TEST_GRAPH_SOLUTIONS.keys())
def example_graph_solution(request):
    return IdentificationTestGraphSolution(**TEST_GRAPH_SOLUTIONS[request.param])
