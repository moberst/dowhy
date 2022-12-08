import logging

from dowhy.causal_refuter import CausalRefuter
from dowhy.causal_refuters.assess_overlap_overrule import OverruleAnalyzer

logger = logging.getLogger(__name__)


class AssessOverlap(CausalRefuter):
    """Assess Overlap

    AssessOverlap class implements the OverRule method from Oberst et al. 2020
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize the parameters required for the refuter.

        This is called with arguments passed through `refute_estimate`

        TODO: Add arguments here
        """
        super().__init__(*args, **kwargs)
        self._backdoor_vars = self._target_estimand.get_backdoor_variables()

    def refute_estimate(self, show_progress_bar=False):
        return assess_support_and_overlap_overrule(
            self._data,
            self._backdoor_vars,
            self._treatment_name,
        )


def assess_support_and_overlap_overrule(data, backdoor_vars, treatment_name):
    X = data[backdoor_vars]
    g = data[treatment_name]
    analyzer = OverruleAnalyzer()
    analyzer.fit(X, g)
    return analyzer