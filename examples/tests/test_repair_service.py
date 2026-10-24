from examples import repair_service as service


def test_history_is_bounded_and_keeps_recent_requests():
    service.reset()
    for i in range(1000):
        service.remember_request(i)
    assert len(service.REQUESTS)<=64,'Request history grows without a bound'
    assert service.REQUESTS[-1]==999


def test_reset_and_return_value():
    service.reset()
    assert service.remember_request('one')==1
    service.reset()
    assert not service.REQUESTS
