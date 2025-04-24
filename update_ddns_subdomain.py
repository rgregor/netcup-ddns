#!/bin/env python3
import ipaddress
import logging
import logging.handlers
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from ipaddress import IPv4Address, IPv6Address
from typing import List, Optional, Tuple

from nc_dnsapi import Client, DNSRecord
from typed_configparser import ConfigParser


@dataclass
class GeneralConfig:
    log_level: int = 20


@dataclass
class NetcupAPIConfig:
    api_key: str
    api_password: str
    customer: str
    timeout: float = 3


@dataclass
class DDNSConfig:
    domain: str
    subdomain: str
    poll_interval_s: float = 30.0
    fetch_ip4_cmd: str = 'curl -s https://4.icanhazip.com'
    fetch_ip6_cmd: str = 'curl -s https://6.icanhazip.com'


@dataclass
class Config:
    general_config: GeneralConfig
    netcup_api_config: NetcupAPIConfig
    ddns_config: DDNSConfig

    def __init__(self, filename: str):
        config_parser = ConfigParser()
        config_parser.read(filename)
        self.general_config = config_parser.parse_section(using_dataclass=GeneralConfig)
        self.netcup_api_config = config_parser.parse_section(using_dataclass=NetcupAPIConfig)
        self.ddns_config = config_parser.parse_section(using_dataclass=DDNSConfig)


class DNSRecordSource:

    @staticmethod
    def _run_fetch_cmd(cmd: str) -> Optional[str]:
        cmd = cmd.strip()
        if cmd:
            log.debug(f"running fetch command: '{cmd}'")
            proc_result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if 0 == proc_result.returncode:
                log.debug(f"fetch cmd success: stdout={proc_result.stdout}")
                return proc_result.stdout
            else:
                log.error(
                    f"fetch cmd:\n\trc={proc_result.returncode}\n\tcmd={cmd}\n\tstdout={proc_result.stdout}\n\tstderr={proc_result.stderr}")
        else:
            log.warning("skipping empty fetch command")
        return None

    @staticmethod
    def _parse_ip4(stdout: str) -> IPv4Address:
        ip4_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}\b'
        ip4_candidates = re.findall(ip4_pattern, stdout)
        for ip4_candidate in ip4_candidates:
            try:
                # Create an IPv4Address object (raises ValueError if invalid)
                ip4 = IPv4Address(ip4_candidate)

                # Check if the address is globally routable (public)
                if (ip4.is_private  # Excludes private IP ranges (like 10.0.0.0/8, 192.168.0.0/16, etc.)
                        or ip4.is_loopback  # Excludes loopback addresses (127.0.0.0/8)
                        or ip4.is_link_local  # Excludes link-local addresses (169.254.0.0/16)
                        or ip4.is_multicast  # Excludes multicast addresses (224.0.0.0/4)
                        or ip4.is_reserved  # Excludes reserved IP ranges (future use or documentation blocks)
                        or ip4 == IPv4Address("0.0.0.0")  # Exclude 0.0.0.0
                        or ip4 == IPv4Address("255.255.255.255")  # Exclude broadcast address
                        or int(ip4_candidate.split('.')[-1]) in [0, 255]):  # subnet addr or broadcast
                    continue
                return ip4
            except ipaddress.AddressValueError as e:
                log.debug(f"Skipping non parseable candidate: {ip4_candidate}: {e}")
                continue
        raise ValueError(
            f"Could not parse valid, inet routable IPv4 address\ncandidates={ip4_candidates}\ninput string={stdout}")

    @staticmethod
    def _parse_ip6(stdout: str) -> IPv6Address:
        # Regex to find potential IPv6 addresses
        ip6_pattern = (r'\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b|'
                       r'\b(?:[0-9a-f]{1,4}:){1,7}:\b|'
                       r'\b::(?:[0-9a-f]{1,4}:){1,7}[0-9a-f]{1,4}\b|'
                       r'\b(?:[0-9a-f]{1,4}:){1,6}::(?:[0-9a-f]{1,4}:){0,6}[0-9a-f]{1,4}\b')
        ip6_candidates = re.findall(ip6_pattern, stdout, re.IGNORECASE)

        for ip6_candidate in ip6_candidates:
            try:
                # Create an IPv6Address object (raises ValueError if invalid)
                ip6 = IPv6Address(ip6_candidate)

                # Check if the address is globally routable (public) and valid
                if (ip6.is_private  # Excludes private IP ranges
                        or ip6.is_loopback  # Excludes loopback addresses (::1)
                        or ip6.is_link_local  # Excludes link-local addresses (fe80::/10)
                        or ip6.is_multicast  # Excludes multicast addresses (ff00::/8)
                        or ip6.is_reserved):  # Excludes reserved IP ranges
                    continue

                return ip6
            except ipaddress.AddressValueError as e:
                log.debug(f"Skipping non parseable candidate: {ip6_candidate}: {e}")
                continue

        # Raise error if no valid public IPv6 address is found
        raise ValueError(
            f"Could not parse valid, inet routable IPv6 address\ncandidates={ip6_candidates}\ninput string={stdout}")

    def __init__(self, config: Config):
        self._config = config
        self._last_polled: float = time.time() - (config.ddns_config.poll_interval_s * 2)
        self._previous_records: List[DNSRecord] = []

    def create_records(self) -> List[DNSRecord]:
        records: List[DNSRecord] = []

        ip4_fetch_output = self._run_fetch_cmd(cmd=self._config.ddns_config.fetch_ip4_cmd)
        if ip4_fetch_output:
            destination_ip4 = self._parse_ip4(ip4_fetch_output)
            records.append(DNSRecord(
                type='A',
                hostname=self._config.ddns_config.subdomain,
                destination=str(destination_ip4)))

        ip6_fetch_output = self._run_fetch_cmd(cmd=self._config.ddns_config.fetch_ip6_cmd)
        if ip6_fetch_output:
            destination_ip6 = self._parse_ip6(ip6_fetch_output)
            records.append(DNSRecord(
                type='AAAA',
                hostname=self._config.ddns_config.subdomain,
                destination=str(destination_ip6)))
        return records

    def wait_for_changed_records(self):
        while True:
            now = time.time()
            if now - self._last_polled < self._config.ddns_config.poll_interval_s:
                log.debug("poll_interval not reached, going to sleep")
                time.sleep(self._last_polled - now + self._config.ddns_config.poll_interval_s)

            new_records = self.create_records()
            self._last_polled = time.time()

            if len(new_records) and not all(record in self._previous_records for record in new_records):
                self._previous_records = new_records
                assert all(record in self._previous_records for record in new_records) and all(
                    record in new_records for record in self._previous_records)
                return new_records
            else:
                log.debug("records unchanged, waiting")


def match_and_update_records(new_records: List[DNSRecord], current_records: List[DNSRecord]) -> Tuple[
    List[DNSRecord], List[DNSRecord]]:
    unmatched_records: List[DNSRecord] = list(new_records)
    updated_records: List[DNSRecord] = []

    for current_record in current_records:
        log.debug(f" {current_record}")
        for unmatched_record in unmatched_records:
            if unmatched_record.hostname == current_record.hostname and unmatched_record.type == current_record.type:
                if unmatched_record.destination != current_record.destination:
                    current_record.destination = unmatched_record.destination
                    updated_records.append(current_record)
                unmatched_records.remove(unmatched_record)
    return updated_records, unmatched_records


def run_main_loop(config: Config, record_source: DNSRecordSource):
    previous_records: List[DNSRecord] = []
    new_records: List[DNSRecord] = previous_records

    while True:
        try:
            if new_records == previous_records:
                if len(previous_records) != 0: log.info("waiting for changed records")
                new_records = record_source.wait_for_changed_records()

            assert len(new_records) > 0
            log.debug(f"Starting DNSRecord update: , previous={previous_records}, new={new_records}")

            with Client(**asdict(config.netcup_api_config)) as client:

                # fetch records
                log.debug(f"fetching DNS Records for domain / zone: {config.ddns_config.domain}")
                current_records = client.dns_records(config.ddns_config.domain)

                updated_records, unmatched_records = match_and_update_records(new_records, current_records)

                if len(updated_records):
                    log.info(f" updating records {updated_records}")
                    client.update_dns_records(config.ddns_config.domain, updated_records)

                for unmatched_record in unmatched_records:
                    log.info(f" creating records {unmatched_record}")
                    client.add_dns_record(config.ddns_config.domain, unmatched_record)

                previous_records = new_records

        except (InterruptedError, KeyboardInterrupt) as ie:
            log.warning(f"received interrupt, shutting down main loop")
            raise ie
        except Exception as e:
            log.error(f"Exception in main loop: {e}")


def create_logger(log_level):
    # Create a logger
    logger = logging.getLogger("[netcup-ddns]")
    logger.setLevel(log_level)  # Set the desired logging level

    # Create a SysLogHandler
    # Note: This may cause issues when running in a container
    # log_handler = logging.handlers.SysLogHandler(
    #    address='/dev/log')  # Default address for Unix-based systems
    log_handler = logging.StreamHandler()

    # Create a formatter and set it for the handler
    formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s')
    log_handler.setFormatter(formatter)

    # Add the SysLogHandler to the logger
    logger.addHandler(log_handler)

    return logger


def handle_sigterm(signum, frame):
    log.debug("Received SIGTERM. Shutting down gracefully...")
    raise InterruptedError("Shutdown requested by SIGTERM")


def handle_sighup(signum, frame):
    log.debug("Received SIGHUP. Shutting down gracefully...")
    raise InterruptedError("Shutdown requested by SIGHUP")


def main():
    # Register signal handlers
    signal.signal(signal.SIGHUP, handle_sighup)
    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        _config = Config('netcup-ddns.conf')
        log.setLevel(_config.general_config.log_level)
        run_main_loop(_config, DNSRecordSource(_config))
    except (InterruptedError, KeyboardInterrupt) as ie:
        log.info("Exiting")
        sys.exit(0)
    except Exception as e:
        log.fatal(f"Unexpected Exception: {e}")
        sys.exit(-1)


# entry point
if __name__ == "__main__":
    log = create_logger(logging.INFO)
    main()
