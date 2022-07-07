from __future__ import annotations

import logging
import os
import platform

from abc import abstractmethod
from collections.abc import Iterable
from typing import Any, TYPE_CHECKING, Callable, Tuple, Dict, Set

from checkov.common.output.github_actions_record import GithubActionsRecord
from checkov.common.output.record import Record
from checkov.common.output.report import Report, CheckType
from checkov.common.parallelizer.parallel_runner import parallel_runner
from checkov.common.runners.base_runner import BaseRunner, filter_ignored_paths
from checkov.common.util.consts import START_LINE, END_LINE
from checkov.runner_filter import RunnerFilter
from checkov.common.util.suppression import collect_suppressions_for_context

if TYPE_CHECKING:
    from checkov.common.checks.base_check_registry import BaseCheckRegistry


class Runner(BaseRunner):
    def __init__(self, workflow_name=None, jobs=None, triggers=None):
        super().__init__()
        self.jobs = jobs
        self.workflow_name = workflow_name
        self.triggers = triggers

    def _load_files(
            self,
            files_to_load: list[str],
            definitions: dict[str, dict[str, Any] | list[dict[str, Any]]],
            definitions_raw: dict[str, list[tuple[int, str]]],
            filename_fn: Callable[[str], str] | None = None,
    ) -> None:
        files_to_load = [filename_fn(file) if filename_fn else file for file in files_to_load]
        results = parallel_runner.run_function(lambda f: (f, self._parse_file(f)), files_to_load)
        for file, result in results:
            if result:
                (definitions[file], definitions_raw[file]) = result
                if self.check_type == CheckType.GITHUB_ACTIONS:
                    self.workflow_name = result[0].get('name')  # type:ignore
                    self.jobs = self._get_jobs(result)
                    self.triggers = self._get_triggers(result)

    @abstractmethod
    def _parse_file(
            self, f: str
    ) -> tuple[dict[str, Any] | list[dict[str, Any]], list[tuple[int, str]]] | None:
        raise Exception("parser should be imported by deriving class")

    def run(
            self,
            root_folder: str | None = None,
            external_checks_dir: list[str] | None = None,
            files: list[str] | None = None,
            runner_filter: RunnerFilter | None = None,
            collect_skip_comments: bool = True,
    ) -> Report:
        runner_filter = runner_filter or RunnerFilter()
        if not runner_filter.show_progress_bar:
            self.pbar.turn_off_progress_bar()

        registry = self.import_registry()

        definitions: dict[str, dict[str, Any] | list[dict[str, Any]]] = {}
        definitions_raw: dict[str, list[tuple[int, str]]] = {}

        report = Report(self.check_type)

        if not files and not root_folder:
            logging.debug("No resources to scan.")
            return report

        if not external_checks_dir and self.require_external_checks():
            logging.debug("The runner requires that external checks are defined.")
            return report
        if external_checks_dir:
            for directory in external_checks_dir:
                registry.load_external_checks(directory)

        if files:
            self._load_files(files, definitions, definitions_raw)

        if root_folder:
            for root, d_names, f_names in os.walk(root_folder):
                filter_ignored_paths(root, d_names, runner_filter.excluded_paths, self.included_paths())
                filter_ignored_paths(root, f_names, runner_filter.excluded_paths, self.included_paths())
                self._load_files(f_names, definitions, definitions_raw, lambda f: os.path.join(root, f))

        self.pbar.initiate(len(definitions))
        for file_path in definitions.keys():
            self.pbar.set_additional_data({'Current File Scanned': os.path.relpath(file_path, root_folder)})
            skipped_checks = collect_suppressions_for_context(definitions_raw[file_path])
            results = registry.scan(file_path, definitions[file_path], skipped_checks, runner_filter)
            for key, result in results.items():
                result_config = result["results_configuration"]
                start = 0
                end = 0
                check = result.pop("check", None)  # use pop to remove Check class which is not serializable from
                if check is None:
                    continue

                # result record
                if result_config:
                    end, start = self.get_start_end_lines(end, result_config, start)
                if platform.system() == "Windows":
                    root_folder = os.path.split(file_path)[0]

                record = Record(
                    check_id=check.id,
                    bc_check_id=check.bc_id,
                    check_name=check.name,
                    check_result=result,
                    code_block=definitions_raw[file_path][start - 1:end + 1],
                    file_path=f"/{os.path.relpath(file_path, root_folder)}",
                    file_line_range=[start, end + 1],
                    resource=self.get_resource(file_path, key, check.supported_entities),
                    # type:ignore[arg-type]  # key is str not BaseCheck
                    evaluations=None,
                    check_class=check.__class__.__module__,
                    file_abs_path=os.path.abspath(file_path),
                    entity_tags=None,
                    severity=check.severity,
                )
                if self.check_type == CheckType.GITHUB_ACTIONS:
                    record = GithubActionsRecord(
                        record=record,
                        jobs=self.jobs,
                        triggers=self.triggers,
                        workflow_name=self.workflow_name
                    )
                report.add_record(record)
            self.pbar.update()
        self.pbar.close()
        return report

    def included_paths(self) -> Iterable[str]:
        return []

    def get_resource(self, file_path: str, key: str, supported_entities: Iterable[str]) -> str:
        return f"{file_path}.{key}"

    @abstractmethod
    def get_start_end_lines(self, end: int, result_config: dict[str, Any], start: int) -> tuple[int, int]:
        raise Exception("should be handled by derived class")

    @abstractmethod
    def import_registry(self) -> BaseCheckRegistry:
        raise Exception("registry should be imported by deriving class")

    def require_external_checks(self) -> bool:
        return True

    @staticmethod
    def _change_files_path_to_relative(report: Report) -> None:
        for record in report.get_all_records():
            record.file_path = record.file_path.replace(os.getcwd(), "")
            record.resource = record.resource.replace(os.getcwd(), "")

    def _get_triggers(self, result: Tuple[Dict[str, Any], Dict[str, Any]]) -> set[str]:
        triggers_set = set()
        triggers = result[0].get(True)
        try:
            if triggers:
                triggers_set = {key for key in triggers.keys() if key != START_LINE and key != END_LINE}

        except Exception as e:
            logging.info(f"error : {str(e)}")
        return triggers_set

    def _get_jobs(self, result: Tuple[Dict[str, Any], Dict[str, Any]]) -> dict[str, dict[str, str]]:
        jobs_dict: dict[str, dict[str, str]] = {}
        tmp_key: str = ""
        jobs = result[0].get('jobs')
        if jobs:
            for key, value in jobs.items():
                if key != START_LINE and key != END_LINE:
                    jobs_dict[key] = {}
                    tmp_key = key
                else:
                    if key == START_LINE:
                        jobs_dict[tmp_key][START_LINE] = value
                    if key == END_LINE:
                        jobs_dict[tmp_key][END_LINE] = value

        return jobs_dict
