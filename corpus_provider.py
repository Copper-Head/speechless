import random
import re
import subprocess
import tarfile
from functools import reduce
from pathlib import Path
from tarfile import *

from collections import Counter
from typing import List, Iterable, Optional, Dict, Callable, Tuple
from urllib import request

from grapheme_enconding import frequent_characters_in_english
from labeled_example import LabeledExample
from tools import mkdir, distinct, name_without_extension, extension, count_summary, group


class ParsingException(Exception):
    pass


class TrainingTestSplit:
    training_only = lambda examples: (examples, [])
    test_only = lambda examples: ([], examples)

    @staticmethod
    def randomly_by_directory(train_set_share: float = .9) -> Callable[
        [List[LabeledExample]], Tuple[List[LabeledExample], List[LabeledExample]]]:
        def split(examples: List[LabeledExample]) -> Tuple[List[LabeledExample], List[LabeledExample]]:
            examples_by_directory = group(examples, key=lambda e: e.audio_directory)
            directories = examples_by_directory.keys()

            # split must be the same every time:
            random.seed(42)
            training_directories = set(random.sample(directories, round(train_set_share * len(directories))))

            training_examples = [example for example in examples if example.audio_directory in training_directories]
            test_examples = [example for example in examples if example.audio_directory not in training_directories]

            return training_examples, test_examples

        return split

    @staticmethod
    def by_directory(test_directory_name: str = "test") -> Callable[
        [List[LabeledExample]], Tuple[List[LabeledExample], List[LabeledExample]]]:
        def split(examples: List[LabeledExample]) -> Tuple[List[LabeledExample], List[LabeledExample]]:
            training_examples = [example for example in examples if example.audio_directory.name == test_directory_name]
            test_examples = [example for example in examples if example.audio_directory.name != test_directory_name]

            return training_examples, test_examples

        return split


class CorpusProvider:
    def __init__(self, base_directory: Path,
                 base_source_url_or_directory: str = "http://www.openslr.org/resources/12/",
                 corpus_names: Iterable[str] = ("dev-clean", "dev-other", "test-clean", "test-other",
                                                "train-clean-100", "train-clean-360", "train-other-500"),
                 tar_gz_extension: str = ".tar.gz",
                 mel_frequency_count: int = 128,
                 root_compressed_directory_name_to_skip: Optional[str] = "LibriSpeech/",
                 subdirectory_depth: int = 3,
                 allowed_characters: List[chr] = frequent_characters_in_english,
                 tags_to_ignore: Iterable[str] = list(),
                 id_filter_regex=re.compile('[\s\S]*'),
                 training_test_split: Callable[[List[LabeledExample]], Tuple[
                     List[LabeledExample], List[LabeledExample]]] = TrainingTestSplit.randomly_by_directory(.9)):
        self.id_filter_regex = id_filter_regex
        self.tags_to_ignore = tags_to_ignore
        self.allowed_characters = allowed_characters
        self.subdirectory_depth = subdirectory_depth
        self.root_compressed_directory_name_to_skip = root_compressed_directory_name_to_skip
        self.base_directory = base_directory
        self.base_url_or_directory = base_source_url_or_directory
        self.tar_gz_extension = tar_gz_extension
        self.mel_frequency_count = mel_frequency_count
        self.corpus_names = corpus_names
        mkdir(base_directory)

        self.corpus_directories = [self._download_and_unpack_if_not_yet_done(corpus_name=corpus_name) for corpus_name in
                                   corpus_names]

        directories = self.corpus_directories
        for i in range(self.subdirectory_depth):
            directories = [subdirectory
                           for directory in directories
                           for subdirectory in directory.iterdir() if subdirectory.is_dir()]

        self.files = [file
                      for directory in directories
                      for file in directory.iterdir() if file.is_file()]

        self.unfiltered_audio_files = [file for file in self.files if
                                       (file.name.endswith(".flac") or file.name.endswith(".wav"))]
        audio_files = [file for file in self.unfiltered_audio_files if
                       self.id_filter_regex.match(name_without_extension(file))]
        self.filtered_out_count = len(self.unfiltered_audio_files) - len(audio_files)

        labels_with_tags_by_id = self._extract_labels_by_id(self.files)
        found_audio_ids = set(name_without_extension(f) for f in audio_files)
        found_label_ids = labels_with_tags_by_id.keys()
        self.audio_ids_without_label = list(found_audio_ids - found_label_ids)
        self.label_ids_without_audio = list(found_label_ids - found_audio_ids)

        def example(audio_file: Path) -> LabeledExample:
            return LabeledExample(audio_file, label_from_id=lambda id: self._remove_tags_to_ignore(
                labels_with_tags_by_id[id]),
                                  mel_frequency_count=self.mel_frequency_count,
                                  original_label_with_tags_from_id=lambda id: labels_with_tags_by_id[id])

        self.examples = sorted(
            [example(file) for file in audio_files if name_without_extension(file) in labels_with_tags_by_id.keys()],
            key=lambda x: x.id)
        self.examples_by_id = dict([(e.id, e) for e in self.examples])

    def _remove_tags_to_ignore(self, text: str) -> str:
        return reduce(lambda text, tag: text.replace(tag, ""), self.tags_to_ignore, text)

    def _download_and_unpack_if_not_yet_done(self, corpus_name: str) -> Path:
        file_name = corpus_name + self.tar_gz_extension
        file_url_or_path = self.base_url_or_directory + file_name

        target_directory = self.base_directory / corpus_name

        if not target_directory.exists():
            tar_file = self._download_if_not_yet_done(file_url_or_path, self.base_directory / file_name)
            self._unpack_tar_if_not_yet_done(tar_file, target_directory=target_directory)

        return target_directory

    def _unpack_tar_if_not_yet_done(self, tar_file: Path, target_directory: Path):
        if not target_directory.is_dir():
            with tarfile.open(str(tar_file), 'r:gz') as tar:
                tar.extractall(str(target_directory),
                               members=self._tar_members_root_directory_skipped_if_specified(tar))

    def _tar_members_root_directory_skipped_if_specified(self, tar: TarFile) -> List[TarInfo]:
        members = tar.getmembers()

        if self.root_compressed_directory_name_to_skip is not None:
            for member in members:
                member.name = member.name.replace(self.root_compressed_directory_name_to_skip, '')

        return members

    def _download_if_not_yet_done(self, source_path_or_url: str, target_path: Path) -> Path:
        if not target_path.is_file():
            print("Downloading corpus {} to {}".format(source_path_or_url, target_path))
            if self.base_url_or_directory.startswith("http"):
                request.urlretrieve(source_path_or_url, str(target_path))
            else:
                try:
                    subprocess.check_output(["scp", source_path_or_url, str(target_path)], stderr=subprocess.STDOUT)
                except subprocess.CalledProcessError as e:
                    raise IOError("Copying failed: " + str(e.output))

        return target_path

    def _extract_labels_by_id(self, files: Iterable[Path]) -> Dict[str, str]:
        label_files = [file for file in files if file.name.endswith(".txt")]
        labels_by_id = dict()
        for label_file in label_files:
            with label_file.open() as f:
                for line in f.readlines():
                    parts = line.split()
                    id = parts[0]
                    label = " ".join(parts[1:])
                    labels_by_id[id] = label.lower()
        return labels_by_id

    def is_allowed(self, label: str) -> bool:
        return all(c in self.allowed_characters for c in label)

    def csv_row(self):
        empty_examples = self.empty_examples()
        return [" ".join(self.corpus_names),
                self.file_type_summary(),
                len(self.unfiltered_audio_files), self.filtered_out_count, self.id_filter_regex,
                len(self.audio_ids_without_label), str(self.audio_ids_without_label[:10]),
                len(self.label_ids_without_audio), self.label_ids_without_audio[:10],
                self.tag_summary(),
                len(self.examples),
                len(self.invalid_examples_texts()), self.invalid_examples_summary(),
                len(empty_examples), [e.id for e in empty_examples[:10]],
                self.duplicate_label_count(), self.most_duplicated_labels()]

    def summary(self) -> str:
        tags_summary = self.tag_summary()

        description = "File types: {}\n{}{}{}{}{} extracted examples, of them {} invalid, {} empty, {} duplicate.\n".format(
            self.file_type_summary(),
            "Out of {} audio files, {} were excluded by regex {}\n".format(
                len(self.unfiltered_audio_files), self.filtered_out_count,
                self.id_filter_regex) if self.filtered_out_count > 0 else "",

            "{} audio files without matching label; will be excluded, e. g. {}.\n".format(
                len(self.audio_ids_without_label), self.audio_ids_without_label[:10]) if len(
                self.audio_ids_without_label) > 0 else "",

            "{} labels without matching audio file; will be excluded, e. g. {}.\n".format(
                len(self.label_ids_without_audio), self.label_ids_without_audio[:10]) if len(
                self.label_ids_without_audio) > 0 else "",

            "Removed label tags: {}\n".format(tags_summary) if tags_summary != "" else "",
            len(self.examples),
            len(self.invalid_examples_texts()),
            self.invalid_examples_summary(),
            len(self.empty_examples()),
            self.duplicate_label_count())

        return " ".join(self.corpus_names) + "\n" + "\n".join("\t" + line for line in description.splitlines())

    def invalid_examples_summary(self):
        return "".join([e + '\n' for e in self.invalid_examples_texts()])

    def original_sample_rate_summary(self):
        return count_summary(self.some_original_sample_rates())

    def tag_summary(self):
        return count_summary(self.tags_from_all_examples())

    def file_type_summary(self):
        return count_summary(self.file_extensions())

    def invalid_examples_texts(self):
        return [
            "Invalid characters {} in {}".format(
                distinct([c for c in x.label if c not in self.allowed_characters]), str(x))
            for x in self.examples if not self.is_allowed(x.label)]

    def some_original_sample_rates(self):
        return [example.original_sample_rate for example in
                random.sample(self.examples, min(50, len(self.examples)))]

    def file_extensions(self):
        return [extension(file)
                for directory in self.corpus_directories
                for file in directory.glob('**/*.*') if file.is_file()]

    def empty_examples(self):
        return [example for example in self.examples if example.label == ""]

    def duplicate_label_count(self):
        return len(self.examples) - len(set(e.label for e in self.examples))

    def most_duplicated_labels(self):
        return Counter([example.label for example in self.examples]).most_common(10)

    def tags_from_all_examples(self):
        return [counted_tag
                for example in self.examples
                for tag in self.tags_to_ignore
                for counted_tag in [tag] * example.tag_count(tag)]
