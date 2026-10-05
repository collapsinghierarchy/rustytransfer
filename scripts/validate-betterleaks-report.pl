#!/usr/bin/env perl
use strict;
use warnings;
use JSON::PP;

sub blocked {
    print STDERR "Betterleaks blocked: $_[0].\n";
    exit 1;
}
my $path = shift @ARGV;
defined $path or blocked('missing report path');
open my $file, '<:raw', $path or blocked('scanner did not produce a readable SARIF report');
local $/;
my $report = eval { JSON::PP->new->utf8->decode(<$file>) };
ref($report) eq 'HASH' or blocked('invalid SARIF report');
($report->{version} // '') eq '2.1.0' or blocked('unexpected SARIF version');
ref($report->{runs}) eq 'ARRAY' && @{$report->{runs}} or blocked('SARIF has no completed runs');
for my $run (@{$report->{runs}}) {
    ref($run) eq 'HASH' or blocked('invalid SARIF run');
    ref($run->{tool}) eq 'HASH' && ref($run->{tool}{driver}) eq 'HASH'
        && lc($run->{tool}{driver}{name} // '') eq 'betterleaks'
        or blocked('report does not identify Betterleaks');
    # SARIF permits absent results for a run with no findings.
    if (exists $run->{results}) {
        ref($run->{results}) eq 'ARRAY' or blocked('invalid SARIF results');
        @{$run->{results}} == 0 or blocked('SARIF contains findings');
    }
    if (exists $run->{invocations}) {
        ref($run->{invocations}) eq 'ARRAY' or blocked('invalid SARIF invocations');
        for my $invocation (@{$run->{invocations}}) {
            ref($invocation) eq 'HASH' or blocked('invalid SARIF invocation');
            exists $invocation->{executionSuccessful}
                && JSON::PP::is_bool($invocation->{executionSuccessful})
                && $invocation->{executionSuccessful}
                or blocked('SARIF records an incomplete or failed invocation');
            for my $key (qw(toolExecutionNotifications toolConfigurationNotifications)) {
                next unless exists $invocation->{$key};
                ref($invocation->{$key}) eq 'ARRAY' or blocked('invalid SARIF notifications');
                for my $notice (@{$invocation->{$key}}) {
                    ref($notice) eq 'HASH' or blocked('invalid SARIF notification');
                    my $level = $notice->{level} // 'warning';
                    $level =~ /^(none|note|warning|error)$/ or blocked('invalid SARIF notification level');
                    $level =~ /^(warning|error)$/ and blocked('SARIF contains a scanner warning or error');
                }
            }
        }
    }
}
